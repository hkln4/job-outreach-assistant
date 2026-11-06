"""The hallucination eval itself: its cases are well formed and their traps
are real (no trap pattern matches what the sources actually say), an honest
letter passes, and a fabricating one is caught - by the trap patterns and by
the sentence check independently. The real-model run is
scripts/hallucination_eval.py (costs money, run by hand)."""
import json
import re

from app.applications.evidence import citable_text
from app.applications.synonyms import fold
from app.evals.hallucination import load_cases, markdown_report, run_case, summarize
from app.models import ProfileItem
from tests.fakes import RoutedLLMProvider

CASES = {case["id"]: case for case in load_cases()}


def test_cases_are_well_formed_and_their_traps_are_not_in_the_sources():
    assert len(CASES) >= 6
    for case in CASES.values():
        pages = case["company"]["pages"]
        assert sum(len(p["text"]) for p in pages) >= 200, case["id"]
        assert case["records"] and case["forbidden_patterns"], case["id"]
        sources = " ".join(p["text"] for p in pages)
        sources += " ".join(citable_text(ProfileItem(extracted=r["extracted"], facts=r.get("facts", {}), owner_facts=[])) for r in case["records"])
        for pattern in case["forbidden_patterns"]:
            assert not re.search(pattern, fold(sources), re.IGNORECASE), (case["id"], pattern)


def _llm(letter: str, claims: list[dict]) -> RoutedLLMProvider:
    meta = {"target_position": "CV Engineer", "cv_choice_name": "Eval CV", "sources": [], "claims": claims}
    return RoutedLLMProvider({
        "planning a job application email": json.dumps({"angles": []}),
        "Output plain text only": letter,
        "annotating a finished job application email": json.dumps(meta),
    })


HONEST = (
    "Subject: Junior Computer Vision Engineer Application\n\n"
    "Hello,\nI am writing because MedScan develops decision-support software for radiologists. "
    "In my graduation project I segmented the left ventricle with a U-Net and reached Dice 0.931 on CAMUS. "
    "I would be glad to talk about how this could fit your team.\nThe CV is attached."
)
HONEST_CLAIMS = [
    {"sentence": "I am writing because MedScan develops decision-support software for radiologists.", "kind": "company",
     "supports": [{"source": "https://medscan.example/", "quote": "MedScan develops decision-support software for radiologists"}]},
    {"sentence": "In my graduation project I segmented the left ventricle with a U-Net and reached Dice 0.931 on CAMUS.", "kind": "owner",
     "supports": [{"source": "E1", "quote": "Dice 0.931 on CAMUS"}]},
    {"sentence": "I would be glad to talk about how this could fit your team.", "kind": "none", "supports": []},
    {"sentence": "The CV is attached.", "kind": "none", "supports": []},
]


def test_an_honest_letter_passes():
    result = run_case(CASES["framing-trap"], _llm(HONEST, HONEST_CLAIMS))
    assert not result.error, result.error
    assert result.scores["passed"], result.scores
    assert result.scores["claim_check_ran"] and result.scores["sentences"] == 4


def test_a_fabricating_letter_is_caught_twice():
    letter = HONEST.replace(
        "I would be glad to talk",
        "My system was deployed in production at two hospitals and served 500 users every day. I would be glad to talk",
    )
    claims = HONEST_CLAIMS[:2] + [
        {"sentence": "My system was deployed in production at two hospitals and served 500 users every day.", "kind": "owner",
         "supports": [{"source": "E1", "quote": "Deployed to production at two hospitals, serving 500 users every day"}]},  # model framing, not citable
    ] + HONEST_CLAIMS[2:]
    result = run_case(CASES["framing-trap"], _llm(letter, claims))
    scores = result.scores
    assert not scores["passed"]
    assert scores["trap_hits"]  # ground truth: the fabrication matched a trap
    assert [u["reason"] for u in scores["unsupported"]] == ["owner_claim_unverified"]  # and the sentence check caught it
    assert "500" in scores["ungrounded"]

    summary = summarize([result, run_case(CASES["framing-trap"], _llm(HONEST, HONEST_CLAIMS))])
    assert summary["pass_rate"] == 0.5 and summary["trap_hit_rate"] == 0.5 and summary["claim_check_coverage"] == 1.0
    report = markdown_report({"at": "2026-11-06T12:00", "summary": summary, "results": [result.__dict__]})
    assert "framing-trap" in report and "owner_claim_unverified" in report


def test_a_failing_pipeline_is_reported_not_raised():
    result = run_case(CASES["summary-trap"], RoutedLLMProvider({"Output plain text only": "no subject line at all"}))
    assert result.error and "subject" in result.error
