"""Phase 0 bug 9: the owner edits a ProfileItem's record; edits survive
re-analysis, which proposes its differing values as a diff instead."""
from fastapi.testclient import TestClient

from app.db import SessionLocal, init_db
from app.main import app
from app.models import CV, ProfileItem, ProfileItemStatus
from app.profile_builder.coverage import record_item
from app.profile_builder.records import RICH_FIELDS, from_form_value, to_form_value

client = TestClient(app)


def setup_module():
    init_db()
    db = SessionLocal()
    try:
        if db.query(CV).first() is None:
            db.add(CV(name="Edit Gate CV", file_path=""))
            db.commit()
    finally:
        db.close()


def _item(db, ref: str) -> ProfileItem:
    return record_item(
        db,
        source_type="site",
        source_ref=ref,
        extracted={"purpose": "A blog", "stack": ["Hugo"], "role": "unknown", "highlights": []},
        source_urls=[ref],
    )


def test_owner_edit_is_saved_and_survives_reanalysis():
    db = SessionLocal()
    try:
        item_id = _item(db, "https://owner.example/edit").id
    finally:
        db.close()

    page = client.get(f"/profile/items/{item_id}")
    assert page.status_code == 200 and "A blog" in page.text
    client.post(
        f"/profile/items/{item_id}/edit",
        data={"purpose": "My ML blog", "stack": "Hugo\nPython", "role": "author", "highlights": ""},
    )

    db = SessionLocal()
    try:
        item = db.get(ProfileItem, item_id)
        assert item.extracted["purpose"] == "My ML blog"
        assert item.extracted["stack"] == ["Hugo", "Python"]
        assert set(item.owner_edited_fields) == {"purpose", "stack", "role"}

        # re-analysis: owner fields kept, differing proposals parked
        record_item(
            db,
            source_type="site",
            source_ref="https://owner.example/edit",
            extracted={"purpose": "A blog about cats", "stack": ["Hugo", "Python"], "role": "unknown", "highlights": ["new"]},
            source_urls=["https://owner.example/edit"],
        )
        db.refresh(item)
        assert item.extracted["purpose"] == "My ML blog"
        assert item.extracted["highlights"] == ["new"]  # not owner-edited -> updated
        assert item.pending_update == {"purpose": "A blog about cats", "role": "unknown"}
    finally:
        db.close()

    page = client.get(f"/profile/items/{item_id}")
    assert "A blog about cats" in page.text
    client.post(f"/profile/items/{item_id}/pending/purpose", data={"action": "accept"})
    client.post(f"/profile/items/{item_id}/pending/role", data={"action": "dismiss"})
    db = SessionLocal()
    try:
        item = db.get(ProfileItem, item_id)
        assert item.extracted["purpose"] == "A blog about cats"
        assert item.extracted["role"] == "author"
        assert item.pending_update == {}
    finally:
        db.close()


def test_resaving_an_untouched_form_is_not_an_edit():
    db = SessionLocal()
    try:
        item = _item(db, "https://owner.example/untouched")
        form = {name: to_form_value(item.extracted.get(name), kind) for name, kind in [("purpose", "textarea"), ("stack", "list"), ("role", "text"), ("highlights", "list")]}
        item_id = item.id
    finally:
        db.close()
    client.post(f"/profile/items/{item_id}/edit", data=form)
    db = SessionLocal()
    try:
        assert db.get(ProfileItem, item_id).owner_edited_fields == []
    finally:
        db.close()


def test_owner_answers_become_owner_facts():
    db = SessionLocal()
    try:
        item = _item(db, "https://owner.example/questions")
        item.extracted = {**item.extracted, "open_questions": ["Solo or team?", "Who used it?"]}
        db.commit()
        item_id = item.id
    finally:
        db.close()
    client.post(f"/profile/items/{item_id}/answers", data={"answer_0": "Solo", "answer_1": "", "extra_fact": "Dice 0.87"})
    db = SessionLocal()
    try:
        facts = db.get(ProfileItem, item_id).owner_facts
        assert [(f["question"], f["answer"], f["source"]) for f in facts] == [("Solo or team?", "Solo", "owner"), ("", "Dice 0.87", "owner")]
    finally:
        db.close()
    page = client.get(f"/profile/items/{item_id}")
    assert "Who used it?" in page.text  # unanswered question still offered


def test_form_values_round_trip_for_every_rich_field_kind():
    record = {
        "one_liner": "Segmentation tool",
        "architecture": ["loader", "U-Net"],
        "stack_used_for": [{"tech": "PyTorch", "for": "U-Net training"}],
        "results": [{"claim": "Dice 0.87", "quote": "Dice | 0.87", "source": "readme"}],
        "role_angles": {"ml_ai": "Trained the model", "backend_data": "unknown"},
    }
    kinds = dict(RICH_FIELDS)
    for field, value in record.items():
        if field == "results":
            continue  # "|" inside a quote is only safe in the last column
        assert from_form_value(to_form_value(value, kinds[field]), kinds[field]) == value
    assert from_form_value("Dice 0.87 | quoted | readme", kinds["results"]) == [{"claim": "Dice 0.87", "quote": "quoted", "source": "readme"}]


def test_item_with_failed_extraction_becomes_analyzed_once_the_owner_fills_it():
    db = SessionLocal()
    try:
        item = ProfileItem(source_type="site", source_ref="https://owner.example/failed", status=ProfileItemStatus.NEEDS_REVIEW,
                           extracted={"purpose": "unknown", "stack": [], "role": "unknown", "highlights": [], "parse_error": "quota"})
        db.add(item)
        db.commit()
        item_id = item.id
    finally:
        db.close()
    client.post(f"/profile/items/{item_id}/edit", data={"purpose": "Portfolio", "stack": "", "role": "unknown", "highlights": ""})
    db = SessionLocal()
    try:
        item = db.get(ProfileItem, item_id)
        assert item.status == ProfileItemStatus.ANALYZED and "parse_error" not in item.extracted
    finally:
        db.close()


def test_writeup_is_generated_from_records_and_flags_unknown_tags():
    from app.models import Profile, ProfileItemStatus
    from app.profile_builder.writeup import generate_writeup
    from tests.fakes import FakeLLMProvider

    db = SessionLocal()
    try:
        item = _item(db, "https://owner.example/writeup")
        assert item.status == ProfileItemStatus.ANALYZED
        llm = FakeLLMProvider(f"# Summary\nWrites a blog [E{item.id}]. Won a prize [E999999].")
        profile = generate_writeup(db, llm)
        assert profile.writeup.startswith("# Summary")
        assert profile.writeup_unknown_tags == [999999]
        assert f"[E{item.id}]" in llm.calls[0]["prompt"]
        assert db.query(Profile).first().writeup == profile.writeup
    finally:
        db.close()
    page = client.get("/profile")
    assert "Writes a blog" in page.text
