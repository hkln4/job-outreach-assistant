"""LLM call logging (CLAUDE.md section 8): every call through the factory's
wrapper becomes one LLMCallLog row tagged with its purpose and ids, and the
/costs page reports it."""
from fastapi.testclient import TestClient

from app.db import SessionLocal, init_db
from app.llm.base import LLMProvider, LLMResponse
from app.llm.call_log import LoggedLLMProvider, cost_summary, cost_usd, llm_context, set_llm_ids
from app.main import app
from app.models import CV, LLMCallLog


class _Usage(LLMProvider):
    def __init__(self, fail: bool = False):
        self.fail = fail

    def generate(self, prompt, *, system=None, temperature=0.2, max_output_tokens=2048, json_schema=None):
        if self.fail:
            raise RuntimeError("boom")
        return LLMResponse(text="{}", model="claude-haiku-4-5", provider="claude", input_tokens=1000, output_tokens=200)


def setup_module():
    init_db()


def _rows(purpose: str) -> list[LLMCallLog]:
    db = SessionLocal()
    try:
        return db.query(LLMCallLog).filter(LLMCallLog.purpose == purpose).all()
    finally:
        db.close()


def test_logged_call_records_purpose_ids_and_tokens():
    llm = LoggedLLMProvider(_Usage(), "claude", "claude-haiku-4-5")

    @llm_context("unit_outer")
    def outer():
        set_llm_ids(company_id=41)
        with llm_context("unit_inner", application_id=7):
            llm.generate("hi")

    outer()
    rows = _rows("unit_inner")
    assert len(rows) == 1
    assert (rows[0].company_id, rows[0].application_id) == (41, 7)
    assert (rows[0].input_tokens, rows[0].output_tokens) == (1000, 200)


def test_failed_call_is_logged_and_still_raises():
    llm = LoggedLLMProvider(_Usage(fail=True), "claude", "claude-haiku-4-5")
    with llm_context("unit_fail"):
        try:
            llm.generate("hi")
        except RuntimeError:
            pass
        else:
            raise AssertionError("the provider error must propagate")
    rows = _rows("unit_fail")
    assert len(rows) == 1 and "boom" in rows[0].error


def test_cost_uses_list_prices_and_reports_unknown_models_as_unpriced():
    assert cost_usd("claude-haiku-4-5", 1_000_000, 1_000_000) == 6.0
    assert cost_usd("claude-haiku-4-5-20251001", 1_000_000, 0) == 1.0  # dated snapshot
    assert cost_usd("some-unknown-model", 10, 10) is None

    db = SessionLocal()
    try:
        db.add(LLMCallLog(purpose="unit_unpriced", model="some-unknown-model", input_tokens=5, output_tokens=5))
        db.commit()
        summary = cost_summary(db)
    finally:
        db.close()
    assert summary["by_purpose"]["unit_unpriced"]["unpriced_tokens"] == 10
    assert summary["by_purpose"]["unit_unpriced"]["cost"] == 0.0


def test_costs_page_renders():
    db = SessionLocal()
    try:
        if db.query(CV).first() is None:  # onboarding gate
            db.add(CV(name="Gate CV", file_path=""))
            db.commit()
    finally:
        db.close()
    response = TestClient(app).get("/costs")
    assert response.status_code == 200
    assert "unit_inner" in response.text
