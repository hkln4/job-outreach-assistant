"""Call sites ask the provider for native structured output (LLMProvider
json_schema), and the app-side parse + validation stays as the safety net."""
import json

import pytest

from app.applications.drafting import DraftResult
from app.applications.evidence import SelectionAnswer
from app.companies.workflow import ResearchAnswer
from app.discovery.triage import TriageResult
from app.preferences.checker import CheckAnswer, check_company
from app.models import Preference, PreferenceType
from tests.fakes import FakeLLMProvider


@pytest.mark.parametrize("model", [DraftResult, SelectionAnswer, CheckAnswer, ResearchAnswer, TriageResult])
def test_answer_models_produce_valid_strict_schemas(model):
    import anthropic

    strict = anthropic.transform_schema(model.model_json_schema())
    assert strict["additionalProperties"] is False


def test_preference_check_sends_its_schema_and_still_parses_text():
    llm = FakeLLMProvider(json.dumps({"flags": [{"preference_id": 1, "evidence_snippet": "defense", "confidence": "high"}]}))
    prefs = [Preference(id=1, description="No defense industry", type=PreferenceType.HARD, signals=[])]

    flags = check_company(llm, "We build radar for defense.", prefs)

    assert flags[0]["preference_id"] == 1
    assert llm.calls[0]["json_schema"] == CheckAnswer.model_json_schema()


def test_preference_check_falls_back_on_unparseable_text():
    llm = FakeLLMProvider("not json")
    prefs = [Preference(id=1, description="No defense industry", type=PreferenceType.HARD, signals=[])]
    assert check_company(llm, "text", prefs) == []
