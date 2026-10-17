from app.applications.draft_checks import draft_warnings
from app.models import Application, ApplicationState


def _app(body, **kwargs):
    defaults = dict(
        subject="Junior Backend Engineer Application",
        cover_letter_body=body,
        sources=["https://acme.example"],
        evidence=[],
        revision_history=[{"reasoning": ""}],
        state=ApplicationState.PENDING_APPROVAL,
    )
    defaults.update(kwargs)
    return Application(**defaults)


def _keys(application):
    return [key for key, _ in draft_warnings(application)]


GOOD_BODY = " ".join(["word"] * 150)


def test_clean_draft_has_no_warnings():
    assert draft_warnings(_app(GOOD_BODY)) == []


def test_flags_length_cliches_sources_and_evidence():
    application = _app(
        "Short. I believe I can contribute.",
        sources=[],
        evidence=[{"item_id": 1, "used": False}],
        revision_history=[
            {
                "dropped_sources": ["https://made-up.example"],
                "evidence_fallback": True,
                "unsupported_claims": ["Led a team of ten"],
            }
        ],
    )
    warnings = dict(draft_warnings(application))
    assert warnings["draft_check.word_count"] == "6"
    assert '"i believe i can contribute"' in warnings["draft_check.cliche"]
    assert warnings["draft_check.dropped_sources"] == "https://made-up.example"
    assert "draft_check.no_sources" in warnings
    assert "draft_check.evidence_fallback" in warnings
    assert "draft_check.no_evidence_used" in warnings
    assert warnings["draft_check.unsupported_claims"] == "Led a team of ten"


def test_empty_letter_is_flagged_and_sent_ones_are_not_checked():
    assert _keys(_app("")) == ["draft_check.empty"]
    assert draft_warnings(_app("", state=ApplicationState.AWAITING_REPLY)) == []
