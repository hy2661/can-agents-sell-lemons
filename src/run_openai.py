"""Send a request file of OpenAI-model requests and save the raw responses.

The OpenAI counterpart of run_batch.py, for pilots. Requests are sent one at
a time (no Batch API), which is simpler and fast enough for a few dozen
requests. If OpenAI models join the full run, switch to OpenAI's Batch API
for its 50% discount.

Pipeline position: build_requests.py -> run_openai.py -> parse_outputs.py

Usage:
    python src/run_openai.py data/requests/requests_pilot-openai_original_<ts>.jsonl --dry-run
    python src/run_openai.py data/requests/requests_pilot-openai_original_<ts>.jsonl

Conventions shared with run_batch.py:
* The API key is read from the environment (OPENAI_API_KEY) by OpenAI().
* --dry-run checks the request file and sends nothing.
* Asks for confirmation before spending, unless --yes.
* Writes a new file in data/raw/ and never overwrites one.
* Saves the full raw response; parsing happens later in parse_outputs.py.

Each result is written to disk as soon as it arrives. If the run is
interrupted, everything finished so far is already saved, and the
remaining requests can be rebuilt and sent as a new file.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import openai

from build_requests import REPO_ROOT, load_configs
from run_batch import load_request_file

# Stop after this many errors in a row. One error can be bad luck, but a
# streak usually means something that affects every request (no credits,
# a model the key can't use), and continuing would only record the same
# failure for every remaining request.
MAX_CONSECUTIVE_ERRORS = 3


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def check_provider(requests: list[dict[str, Any]]) -> None:
    """Refuse request files meant for another provider, so an Anthropic file
    is never sent to OpenAI by mistake (it would fail on every request)."""
    providers = {r["metadata"].get("provider") for r in requests}
    if providers != {"openai"}:
        raise ValueError(f"run_openai.py sends only OpenAI requests; this file has {providers}")


def send_one(client: openai.OpenAI, params: dict[str, Any]) -> dict[str, Any]:
    """Send one request and return a result dict shaped like run_batch.py's.

    "succeeded" means the API returned a response, not that the response is
    usable. A response cut off by max_output_tokens has status "incomplete",
    and a refusal shows up inside the output. Both are kept as outcomes and
    sorted out by parse_outputs.py. "errored" means no response came back
    even after the SDK's automatic retries.
    """
    try:
        response = client.responses.create(**params)
    except openai.AuthenticationError:
        raise  # a bad key fails every request, so stop the run instead
    except openai.APIStatusError as e:  # 4xx/5xx after retries
        return {"type": "errored",
                "error": {"status_code": e.status_code, "message": str(e)}}
    except openai.APIConnectionError as e:
        return {"type": "errored", "error": {"status_code": None, "message": str(e)}}
    # model_dump keeps everything, including the reasoning summary items and
    # the exact model name OpenAI reports (these aliases can't be pinned, so
    # that name is our record of what actually answered).
    return {"type": "succeeded", "response": response.model_dump(mode="json")}


def run(requests: list[dict[str, Any]], request_file: Path, raw_dir: Path) -> Path:
    """Send every request in order, writing each result as it arrives."""
    client = openai.OpenAI()  # reads OPENAI_API_KEY from the environment
    started = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    # requests_pilot-x_<ts>__openai.jsonl -> results_pilot-x_<ts>__openai_<started>.jsonl
    # (older request files have no provider suffix, so add it if missing)
    stem = request_file.stem.replace("requests", "results", 1)
    if not stem.endswith("__openai"):
        stem += "__openai"
    out_path = raw_dir / f"{stem}_{started}.jsonl"
    raw_dir.mkdir(parents=True, exist_ok=True)

    consecutive_errors = 0
    with out_path.open("x", encoding="utf-8") as f:  # "x": never overwrite
        for i, r in enumerate(requests, start=1):
            result = send_one(client, r["params"])
            row = {
                "custom_id": r["custom_id"],
                "metadata": r["metadata"],
                "request_file": str(request_file.resolve().relative_to(REPO_ROOT)),
                "retrieved_at": utc_now(),
                "result": result,
            }
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
            f.flush()  # on disk now, in case the run is interrupted
            status = result["response"]["status"] if result["type"] == "succeeded" else "errored"
            print(f"[{i}/{len(requests)}] {r['custom_id']}: {status}", flush=True)

            consecutive_errors = consecutive_errors + 1 if result["type"] == "errored" else 0
            if consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                print(f"\nStopped after {consecutive_errors} errors in a row. Last error:\n"
                      f"  {result['error']['message']}\n"
                      f"Fix the cause, then rebuild and send the requests again.",
                      file=sys.stderr)
                break
    return out_path


def print_summary(out_path: Path) -> None:
    """Response status per model, plus token totals.

    No cost is computed here: OpenAI's official prices for these models
    weren't available when this was written, so check spending in the
    OpenAI dashboard. The tokens are saved with every result for the
    README's cost table.
    """
    rows = [json.loads(line) for line in out_path.read_text(encoding="utf-8").splitlines()]
    statuses: Counter = Counter()
    tokens: dict[str, Counter] = {}
    reported_models: dict[str, set] = {}
    for row in rows:
        model = row["metadata"]["model"]
        result = row["result"]
        if result["type"] != "succeeded":
            statuses[(model, "errored")] += 1
            continue
        response = result["response"]
        statuses[(model, response["status"])] += 1
        usage = response["usage"]
        t = tokens.setdefault(model, Counter())
        t["n"] += 1
        t["input"] += usage["input_tokens"]
        t["output"] += usage["output_tokens"]
        t["reasoning"] += usage["output_tokens_details"]["reasoning_tokens"]
        reported_models.setdefault(model, set()).add(response["model"])

    print("\nResults:")
    for (model, status), n in sorted(statuses.items()):
        print(f"  {model:<16} {status:<12} {n}")
    print("\nTokens (output includes reasoning):")
    for model, t in tokens.items():
        print(f"  {model:<16} in={t['input']:<8} out={t['output']:<8} "
              f"of which reasoning={t['reasoning']:<8} avg_out={t['output'] / t['n']:.0f}")
        print(f"  {'':<16} model name reported by OpenAI: {sorted(reported_models[model])}")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Send OpenAI-model requests one at a time.")
    parser.add_argument("request_file", type=Path, help="JSONL file written by build_requests.py.")
    parser.add_argument("--dry-run", action="store_true", help="Check the file; send nothing.")
    parser.add_argument("--yes", action="store_true", help="Skip the confirmation prompt.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    experiment = load_configs()["experiment"]
    raw_dir = REPO_ROOT / experiment["paths"]["raw_dir"]

    requests = load_request_file(args.request_file)
    check_provider(requests)
    counts = Counter(r["metadata"]["model"] for r in requests)
    print(f"{len(requests)} OpenAI requests: " + ", ".join(f"{m} n={n}" for m, n in counts.items()))

    if args.dry_run:
        print("DRY RUN: nothing was sent.")
        return 0
    if not args.yes and input("Send these requests? [y/N] ").strip().lower() != "y":
        print("Not sent.")
        return 1

    try:
        out_path = run(requests, args.request_file, raw_dir)
    except openai.AuthenticationError:
        print("Authentication failed: set OPENAI_API_KEY in your environment.", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nInterrupted. Results so far are saved in data/raw/.", file=sys.stderr)
        return 130

    print(f"\nWrote results to {out_path.relative_to(REPO_ROOT)}")
    print_summary(out_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
