"""Run the hallucination eval against the real draft model (costs money).

    .venv/bin/python scripts/hallucination_eval.py            # every case, once
    .venv/bin/python scripts/hallucination_eval.py --runs 3 --case framing-trap

Uses LLM_PROVIDER and the draft model from .env. Each case runs in its own
in-memory database (your data is untouched); the calls are logged on /costs.
Roughly 3 draft-model calls per case and run (angles, letter, metadata).
Writes data/evals/hallucination-<time>.json and .md, prints the summary, and
exits 1 when the pass rate is below --min-pass-rate.
"""
import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.evals.hallucination import load_cases, markdown_report, run_eval  # noqa: E402
from app.llm.factory import get_draft_llm_provider  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--runs", type=int, default=1, help="repeat each case (models vary run to run)")
    parser.add_argument("--case", action="append", help="only these case ids (repeatable)")
    parser.add_argument("--min-pass-rate", type=float, default=1.0)
    parser.add_argument("--out", default="data/evals")
    args = parser.parse_args()

    cases = [c for c in load_cases() if not args.case or c["id"] in args.case]
    if not cases:
        print("No matching cases.")
        return 2
    report = run_eval(cases, get_draft_llm_provider(), runs=args.runs)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    (out / f"hallucination-{stamp}.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    markdown = markdown_report(report)
    (out / f"hallucination-{stamp}.md").write_text(markdown, encoding="utf-8")
    print(markdown.split("\n| case")[0])
    print(f"Full report: {out / f'hallucination-{stamp}.md'}")
    return 0 if report["summary"]["pass_rate"] >= args.min_pass_rate else 1


if __name__ == "__main__":
    raise SystemExit(main())
