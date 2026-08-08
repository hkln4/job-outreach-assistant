import json

from app.preferences.checker import check_company
from app.models import Preference, PreferenceType
from tests.fakes import FakeLLMProvider


def test_check_company_returns_empty_without_preferences():
    llm = FakeLLMProvider(json.dumps({"flags": []}))
    assert check_company(llm, "some company text", []) == []
    assert llm.calls == []  # no LLM call needed with nothing to check against


def test_check_company_parses_flags():
    pref = Preference(id=1, description="Avoid defense industry", type=PreferenceType.HARD, signals=["defense"])
    llm = FakeLLMProvider(
        json.dumps(
            {
                "flags": [
                    {"preference_id": 1, "evidence_snippet": "we supply the defense sector", "confidence": "high"}
                ]
            }
        )
    )
    flags = check_company(llm, "we supply the defense sector with software", [pref])
    assert len(flags) == 1
    assert flags[0]["preference_id"] == 1
    assert flags[0]["confidence"] == "high"
