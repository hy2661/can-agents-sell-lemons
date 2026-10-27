"""Submit a request file to the Message Batches API and save the raw results.

STUB: signatures and docstrings only. To be implemented by the author.

Pipeline position: build_requests.py -> run_batch.py -> parse_outputs.py

Usage (planned):
    python src/run_batch.py data/requests/requests_pilot_<ts>.jsonl --dry-run
    python src/run_batch.py data/requests/requests_pilot_<ts>.jsonl
    python src/run_batch.py --resume <batch_id>   # re-download an existing batch

Conventions to follow (see build_requests.py):
* Read ANTHROPIC_API_KEY from the environment. anthropic.Anthropic() does
  this automatically; never pass a key in code.
* --dry-run loads and validates the request file, prints a summary and
  estimated cost, and exits without creating a client.
* Never overwrite anything in data/raw/. Open output files with mode "x".
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any, Iterator


def load_request_file(path: Path) -> list[dict[str, Any]]:
    """Read a JSONL request file written by build_requests.py.

    Check that every custom_id is unique. Batch results come back in any
    order and are matched to requests only by custom_id.
    """
    raise NotImplementedError


def estimate_cost(requests: list[dict[str, Any]]) -> float:
    """Rough upper-bound cost in USD for the batch, printed before submitting.

    Suggested approach: count input tokens for one request per (model,
    condition) with client.messages.count_tokens, assume a typical output
    length from the pilot, multiply by the prices in experiment.yaml ->
    pricing, then apply its batch_discount.
    """
    raise NotImplementedError


def submit_batch(requests: list[dict[str, Any]]) -> str:
    """Create the batch and return its batch ID.

    Send only custom_id and params for each request. The metadata stays local.
    Immediately record the batch ID, the request file path, and the submit
    time in a small manifest under data/raw/ so an interrupted run can be
    resumed with --resume instead of being paid for twice.
    """
    raise NotImplementedError


def wait_for_batch(batch_id: str, poll_seconds: int = 60) -> None:
    """Poll client.messages.batches.retrieve(batch_id) until
    processing_status == "ended", printing request_counts as it goes."""
    raise NotImplementedError


def iter_results(batch_id: str) -> Iterator[dict[str, Any]]:
    """Yield one plain dict per result from client.messages.batches.results.

    Keep everything: result type (succeeded / errored / canceled / expired),
    the full message content including thinking blocks, stop_reason,
    stop_details (set when stop_reason == "refusal"), and usage. Use
    message.model_dump() so nothing is lost. Parsing happens later.
    """
    raise NotImplementedError


def save_raw_results(
    results: Iterator[dict[str, Any]],
    requests: list[dict[str, Any]],
    out_dir: Path,
) -> Path:
    """Write one JSONL line per result to a new timestamped file in data/raw/.

    Join each result to its request metadata by custom_id, and add the batch
    ID and a retrieval timestamp. Record token usage on every line; the
    README's cost table is computed from these.
    """
    raise NotImplementedError


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """CLI: positional request file, --dry-run, --resume BATCH_ID."""
    raise NotImplementedError


def main(argv: list[str] | None = None) -> int:
    raise NotImplementedError


if __name__ == "__main__":
    raise SystemExit(main())
