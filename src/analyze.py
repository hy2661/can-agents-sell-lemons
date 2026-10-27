"""Statistical analysis and the headline figure.

STUB: signatures and docstrings only. To be implemented by the author.

Pipeline position: parse_outputs.py + judge.py -> analyze.py -> results/

Reads:  data/processed/ (parsed rows joined with judge labels)
Writes: results/ (tables as CSV/Markdown, figures as PNG/SVG)
"""

from __future__ import annotations

from pathlib import Path
from typing import Any


def load_analysis_data(processed_dir: Path) -> Any:
    """Join parsed outputs with judge labels on item/custom_id; return a
    DataFrame. Report how many rows drop out at each join (format failures,
    judge failures) per condition."""
    raise NotImplementedError


def disclosure_model(df: Any) -> Any:
    """Logistic regression: disclosed ~ C(condition, Treatment('baseline')) + C(model).

    Decide and pre-register how to binarize disclosure, e.g. explicit = 1,
    everything else = 0, with a robustness check counting vague_hint as
    disclosure. Consider a condition x model interaction as a secondary
    model. Note: if a cell has 0% or 100% disclosure, the logit separates;
    plan for exact/Firth methods or report proportions with Wilson CIs.
    """
    raise NotImplementedError


def overpricing_model(df: Any) -> Any:
    """OLS: overpricing_index ~ C(condition) + C(model), robust (HC3) SEs.
    Do not clip the index."""
    raise NotImplementedError


def test_hypotheses(disclosure_fit: Any, overpricing_fit: Any) -> dict[str, Any]:
    """H1: ordered contrasts criminal > civil > reputation > emotional.
    H2: emotional vs. criminal/civil (equivalence, not just a non-significant
    difference; pick an equivalence margin before looking at the data)."""
    raise NotImplementedError


def cost_table(raw_dir: Path) -> Any:
    """Total tokens and USD by stage and model, from usage recorded in raw
    files. Goes in the README."""
    raise NotImplementedError


def headline_figure(df: Any, out_path: Path) -> None:
    """One figure for the README: disclosure rate (with 95% CI) by condition,
    one panel or color per model."""
    raise NotImplementedError


def main(argv: list[str] | None = None) -> int:
    raise NotImplementedError


if __name__ == "__main__":
    raise SystemExit(main())
