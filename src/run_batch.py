"""Submit a request file to the Message Batches API and save the raw results.

Pipeline position: build_requests.py -> run_batch.py -> parse_outputs.py

Usage:
    python src/run_batch.py data/requests/requests_pilot_<ts>.jsonl --dry-run
    python src/run_batch.py data/requests/requests_pilot_<ts>.jsonl
    python src/run_batch.py --resume <batch_id>

The pilot/full choice is made when the request file is built
(build_requests.py --pilot), so this script needs no --pilot flag: a pilot
request file produces a pilot batch.

What gets written (all under data/raw/, never overwritten):
    batch_<batch_id>.manifest.json   written the moment the batch is created
    <results file>.jsonl             one line per request, written at the end

The manifest is the safety net. Once a batch exists, Anthropic charges for
it whether or not this script survives. If the script is interrupted
(Ctrl-C, closed laptop, network drop), `--resume <batch_id>` uses the
manifest to find the request file again and collect the results, instead of
paying to submit a second time.

The API key is read from the environment by anthropic.Anthropic() (usually
ANTHROPIC_API_KEY). It is never passed or stored in code.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

import anthropic

from build_requests import CUSTOM_ID_PATTERN, REPO_ROOT, load_configs

# Rough characters-per-token ratio for English text, used only for the
# pre-submission cost ESTIMATE. Input tokens are a small share of the cost
# here (output and thinking dominate), so a rough figure is good enough. The
# actual cost is computed afterwards from the usage the API reports.
CHARS_PER_TOKEN = 4


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Loading and checking requests
# ---------------------------------------------------------------------------

def load_request_file(path: Path) -> list[dict[str, Any]]:
    """Read a JSONL request file written by build_requests.py.

    Fails loudly if any custom_id is invalid or repeated. Batch results come
    back in arbitrary order and are matched to requests only by custom_id, so
    a duplicate would make two results impossible to tell apart.
    """
    requests = []
    with path.open("r", encoding="utf-8") as f:
        for line_number, line in enumerate(f, start=1):
            if not line.strip():
                continue
            record = json.loads(line)
            for key in ("custom_id", "params", "metadata"):
                if key not in record:
                    raise ValueError(f"{path}:{line_number}: missing {key!r}")
            requests.append(record)

    seen = set()
    for r in requests:
        custom_id = r["custom_id"]
        if not CUSTOM_ID_PATTERN.match(custom_id):
            raise ValueError(f"Invalid custom_id {custom_id!r}")
        if custom_id in seen:
            raise ValueError(f"Duplicate custom_id {custom_id!r}")
        seen.add(custom_id)

    if not requests:
        raise ValueError(f"{path} contains no requests")
    return requests


# ---------------------------------------------------------------------------
# Cost
# ---------------------------------------------------------------------------

def model_prices(pricing: dict[str, Any], model: str) -> dict[str, float]:
    """Per-million-token batch prices (input, output) for one model."""
    table = pricing["usd_per_million_tokens"]
    if model not in table:
        raise KeyError(f"No price for {model!r} in experiment.yaml -> pricing")
    discount = pricing["batch_discount"]
    return {"input": table[model]["input"] * discount,
            "output": table[model]["output"] * discount}


def estimate_cost(requests: list[dict[str, Any]], pricing: dict[str, Any],
                  assumed_output_tokens: int) -> dict[str, float]:
    """Rough cost estimate in USD, per model, printed before submitting.

    Input tokens are estimated from the character count of the system prompt
    and messages. Output tokens (which include thinking) can't be known in
    advance, so every request is assumed to produce `assumed_output_tokens`.
    Set that from the pilot's measured average before the full run.
    """
    costs: dict[str, float] = {}
    for r in requests:
        params = r["params"]
        model = params["model"]
        chars = len(params.get("system", "")) + sum(
            len(m["content"]) for m in params["messages"])
        prices = model_prices(pricing, model)
        cost = (chars / CHARS_PER_TOKEN * prices["input"]
                + assumed_output_tokens * prices["output"]) / 1_000_000
        costs[model] = costs.get(model, 0.0) + cost
    return costs


def actual_cost(rows: list[dict[str, Any]], pricing: dict[str, Any]) -> dict[str, Any]:
    """Tokens and USD per model, from the usage the API reported.

    Only succeeded requests carry usage; errored, canceled, and expired
    requests are not billed. No prompt caching is used in this project, so
    input_tokens and output_tokens cover the whole bill.
    """
    summary: dict[str, dict[str, float]] = {}
    for row in rows:
        result = row["result"]
        if result["type"] != "succeeded":
            continue
        message = result["message"]
        usage = message["usage"]
        # Price by the model the request asked for (metadata), which is
        # also what the README reports.
        model = row["metadata"]["model"]
        prices = model_prices(pricing, model)
        s = summary.setdefault(model, {"n": 0, "input_tokens": 0, "output_tokens": 0, "usd": 0.0})
        s["n"] += 1
        s["input_tokens"] += usage["input_tokens"]
        s["output_tokens"] += usage["output_tokens"]
        s["usd"] += (usage["input_tokens"] * prices["input"]
                     + usage["output_tokens"] * prices["output"]) / 1_000_000
    return summary


# ---------------------------------------------------------------------------
# Talking to the Batch API
# ---------------------------------------------------------------------------

def submit_batch(client: anthropic.Anthropic, requests: list[dict[str, Any]],
                 request_file: Path, raw_dir: Path) -> str:
    """Create the batch, write its manifest, and return the batch ID.

    Only custom_id and params are sent. The metadata stays local, both
    because the API doesn't accept extra fields and because nothing about
    the experiment's structure needs to leave this machine.
    """
    batch = client.messages.batches.create(
        requests=[{"custom_id": r["custom_id"], "params": r["params"]} for r in requests]
    )
    # Write the manifest immediately, before anything else can fail.
    manifest = {
        "batch_id": batch.id,
        "request_file": str(request_file.resolve().relative_to(REPO_ROOT)),
        "n_requests": len(requests),
        "models": sorted({r["metadata"]["model"] for r in requests}),
        "submitted_at": utc_now(),
        "expires_at": batch.expires_at.isoformat(),
    }
    raw_dir.mkdir(parents=True, exist_ok=True)
    with manifest_path(raw_dir, batch.id).open("x", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)
    return batch.id


def manifest_path(raw_dir: Path, batch_id: str) -> Path:
    return raw_dir / f"batch_{batch_id}.manifest.json"


def wait_for_batch(client: anthropic.Anthropic, batch_id: str, poll_seconds: int = 60) -> None:
    """Poll until the batch has finished processing.

    Most batches finish within an hour; the API's limit is 24 hours, after
    which unfinished requests are marked "expired".
    """
    while True:
        batch = client.messages.batches.retrieve(batch_id)
        c = batch.request_counts
        print(f"[{utc_now()}] {batch.processing_status}: processing={c.processing} "
              f"succeeded={c.succeeded} errored={c.errored} "
              f"canceled={c.canceled} expired={c.expired}", flush=True)
        if batch.processing_status == "ended":
            return
        time.sleep(poll_seconds)


def iter_results(client: anthropic.Anthropic, batch_id: str) -> Iterator[dict[str, Any]]:
    """Yield each result as a plain dict, with nothing dropped.

    model_dump(mode="json") keeps the full message: every content block
    (including the thinking summary), stop_reason, stop_details (set on
    refusals), the model that actually answered, and usage. Parsing happens
    later, in parse_outputs.py, so the raw file stays a faithful record.
    """
    for result in client.messages.batches.results(batch_id):
        yield result.model_dump(mode="json")


def save_raw_results(results: Iterator[dict[str, Any]], requests: list[dict[str, Any]],
                     batch_id: str, request_file: Path, raw_dir: Path) -> tuple[Path, list[dict[str, Any]]]:
    """Write one JSONL line per result to a new file in data/raw/.

    Each line is the request's metadata plus the raw API result, joined by
    custom_id, plus provenance fields. Opening with mode "x" means an
    existing results file can never be overwritten.
    """
    metadata_by_id = {r["custom_id"]: r["metadata"] for r in requests}
    # requests_pilot_20261027T120000Z.jsonl -> results_pilot_20261027T120000Z__<batch_id>.jsonl
    stem = re.sub(r"^requests", "results", request_file.stem)
    out_path = raw_dir / f"{stem}__{batch_id}.jsonl"

    retrieved_at = utc_now()
    rows = []
    with out_path.open("x", encoding="utf-8") as f:
        for result in results:
            custom_id = result["custom_id"]
            row = {
                "custom_id": custom_id,
                "metadata": metadata_by_id[custom_id],
                "batch_id": batch_id,
                "request_file": str(request_file.resolve().relative_to(REPO_ROOT)),
                "retrieved_at": retrieved_at,
                "result": result["result"],
            }
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
            rows.append(row)

    # Every request should come back exactly once. A shortfall means the
    # results download was cut off, so the file is incomplete.
    missing = set(metadata_by_id) - {row["custom_id"] for row in rows}
    if missing:
        print(f"WARNING: {len(missing)} requests have no result in {out_path.name}.",
              file=sys.stderr)
    return out_path, rows


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def print_request_summary(requests: list[dict[str, Any]], estimate: dict[str, float],
                          assumed_output_tokens: int) -> None:
    counts: dict[str, int] = {}
    for r in requests:
        counts[r["metadata"]["model"]] = counts.get(r["metadata"]["model"], 0) + 1
    print(f"{len(requests)} requests. Estimated cost at batch prices, assuming "
          f"{assumed_output_tokens} output tokens per request:")
    for model, n in counts.items():
        print(f"  {model:<22} n={n:<6} ~${estimate[model]:.2f}")
    print(f"  {'TOTAL':<22} {'':<8} ~${sum(estimate.values()):.2f}")


def print_result_summary(rows: list[dict[str, Any]], pricing: dict[str, Any]) -> None:
    """Result types and stop reasons per model, then actual cost.

    Refusals and max_tokens stops are shown here so they're noticed right
    away. They are kept in the data and analysed as outcomes, not retried.
    """
    tally: dict[tuple[str, str], int] = {}
    for row in rows:
        result = row["result"]
        outcome = result["type"]
        if outcome == "succeeded":
            outcome = f"succeeded/{result['message']['stop_reason']}"
        key = (row["metadata"]["model"], outcome)
        tally[key] = tally.get(key, 0) + 1
    print("\nResults:")
    for (model, outcome), n in sorted(tally.items()):
        print(f"  {model:<22} {outcome:<28} {n}")

    cost = actual_cost(rows, pricing)
    print("\nActual cost (batch prices):")
    for model, s in cost.items():
        # Average output per request is the number to copy into
        # experiment.yaml -> pricing.assumed_output_tokens after the pilot.
        avg_out = s["output_tokens"] / s["n"]
        print(f"  {model:<22} in={s['input_tokens']:<9} out={s['output_tokens']:<10} "
              f"avg_out={avg_out:<8.0f} ${s['usd']:.2f}")
    print(f"  {'TOTAL':<22} ${sum(s['usd'] for s in cost.values()):.2f}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Submit a request file to the Batch API.")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("request_file", nargs="?", type=Path,
                        help="JSONL file written by build_requests.py.")
    source.add_argument("--resume", metavar="BATCH_ID",
                        help="Collect results for a batch that was already submitted.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Check the request file and print the cost estimate; send nothing.")
    parser.add_argument("--yes", action="store_true",
                        help="Submit without the interactive confirmation prompt.")
    parser.add_argument("--assumed-output-tokens", type=int, default=None,
                        help="Override experiment.yaml -> pricing.assumed_output_tokens.")
    parser.add_argument("--poll-seconds", type=int, default=60)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    experiment = load_configs()["experiment"]
    pricing = experiment["pricing"]
    raw_dir = REPO_ROOT / experiment["paths"]["raw_dir"]
    assumed_output_tokens = args.assumed_output_tokens or pricing["assumed_output_tokens"]

    # Work out which batch and request file we are dealing with.
    if args.resume:
        manifest = json.loads(manifest_path(raw_dir, args.resume).read_text(encoding="utf-8"))
        request_file = REPO_ROOT / manifest["request_file"]
        batch_id = args.resume
    else:
        request_file = args.request_file
        batch_id = None

    requests = load_request_file(request_file)

    if batch_id is None:
        estimate = estimate_cost(requests, pricing, assumed_output_tokens)
        print_request_summary(requests, estimate, assumed_output_tokens)
        if args.dry_run:
            print("\nDRY RUN: nothing was sent.")
            return 0
        # Submitting spends money, so require an explicit yes unless --yes.
        if not args.yes and input("\nSubmit this batch? [y/N] ").strip().lower() != "y":
            print("Not submitted.")
            return 1
    elif args.dry_run:
        print(f"DRY RUN: would collect results for batch {batch_id}.")
        return 0

    client = anthropic.Anthropic()  # reads the API key from the environment
    try:
        if batch_id is None:
            batch_id = submit_batch(client, requests, request_file, raw_dir)
            print(f"\nSubmitted batch {batch_id}. "
                  f"Manifest: {manifest_path(raw_dir, batch_id).relative_to(REPO_ROOT)}")
        wait_for_batch(client, batch_id, args.poll_seconds)
        out_path, rows = save_raw_results(
            iter_results(client, batch_id), requests, batch_id, request_file, raw_dir)
    except KeyboardInterrupt:
        if batch_id:
            print(f"\nInterrupted. The batch keeps running on Anthropic's side. Resume with:\n"
                  f"  python src/run_batch.py --resume {batch_id}")
        return 130
    except FileExistsError as e:
        print(f"Results for batch {batch_id} were already saved ({e.filename}); "
              f"nothing overwritten.", file=sys.stderr)
        return 1
    except anthropic.AuthenticationError:
        print("Authentication failed: set ANTHROPIC_API_KEY in your environment.", file=sys.stderr)
        return 1
    except anthropic.APIConnectionError as e:
        # The SDK has already retried. If the batch was created, resume later.
        hint = f" Resume with --resume {batch_id}." if batch_id else ""
        print(f"Could not reach the API: {e}.{hint}", file=sys.stderr)
        return 1

    print(f"\nWrote {len(rows)} results to {out_path.relative_to(REPO_ROOT)}")
    print_result_summary(rows, pricing)
    return 0


if __name__ == "__main__":
    sys.exit(main())
