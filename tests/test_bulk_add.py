"""Bulk company add: the flexible line parser + the parse/create endpoints."""
from fastapi.testclient import TestClient

from app.companies.bulk_parse import parse_bulk_text, parse_line
from app.db import SessionLocal, init_db
from app.main import app
from app.models import CV

JSON_ACCEPT = {"Accept": "application/json"}


def setup_module():
    init_db()
    # the onboarding gate (main.py) redirects everywhere until a CV exists
    db = SessionLocal()
    try:
        if db.query(CV).first() is None:
            db.add(CV(name="Bulk Test CV", file_path="data/cv_variants/test_bulk.pdf"))
            db.commit()
    finally:
        db.close()


client = TestClient(app)


# --- parser ---

def test_parse_equals_format():
    row = parse_line("BeeVision= https://www.beevision.com.tr")
    assert row == {
        "raw": "BeeVision= https://www.beevision.com.tr",
        "name": "BeeVision",
        "website_url": "https://www.beevision.com.tr",
        "contact_email": "",
        "error": "",
    }


def test_parse_dotted_name_is_not_eaten_as_url():
    row = parse_line("invent.ai= https://www.invent.ai")
    assert row["name"] == "invent.ai"
    assert row["website_url"] == "https://www.invent.ai"


def test_parse_pipe_with_bare_domain():
    row = parse_line("3Y Teknoloji | 3yteknoloji.com.tr")
    assert row["name"] == "3Y Teknoloji"
    assert row["website_url"] == "https://3yteknoloji.com.tr"


def test_parse_tab_separated_spreadsheet_paste():
    row = parse_line("Jotform\thttps://www.jotform.com")
    assert row["name"] == "Jotform"
    assert row["website_url"] == "https://www.jotform.com"


def test_parse_www_url_without_scheme():
    row = parse_line("Alyo Bilişim= www.alyo.com.tr")
    assert row["name"] == "Alyo Bilişim"
    assert row["website_url"] == "https://www.alyo.com.tr"


def test_parse_email_on_line_becomes_contact_email():
    row = parse_line("adesso, https://www.adesso.com.tr, jobs@adesso.com.tr")
    assert row["name"] == "adesso"
    assert row["website_url"] == "https://www.adesso.com.tr"
    assert row["contact_email"] == "jobs@adesso.com.tr"


def test_parse_name_with_slash_kept_whole():
    row = parse_line("Ratio Energy / Ratio Technologies= https://www.ratiotechnologies.com.tr")
    assert row["name"] == "Ratio Energy / Ratio Technologies"


def test_parse_missing_website_is_flagged_not_dropped():
    rows = parse_bulk_text("Openzeka= Doğrulanamadı\nBeeVision= https://www.beevision.com.tr")
    assert len(rows) == 2
    assert rows[0]["name"] == "Openzeka"
    assert rows[0]["error"] == "missing_website"
    assert rows[1]["error"] == ""


def test_parse_flags_in_batch_duplicates():
    text = (
        "BeeVision= https://www.beevision.com.tr\n"
        "beevision | https://beevision.com.tr\n"  # same name + same domain
        "Other Co= https://other.example\n"
    )
    rows = parse_bulk_text(text)
    assert [r["error"] for r in rows] == ["", "duplicate_in_batch", ""]


def test_parse_skips_blank_lines():
    rows = parse_bulk_text("\n\nBeeVision= https://www.beevision.com.tr\n\n")
    assert len(rows) == 1


# --- endpoints ---

def test_create_company_json_response_and_duplicate_409():
    r = client.post("/companies", data={"name": "JsonCo Bulk"}, headers=JSON_ACCEPT)
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "created"
    assert data["company"]["name"] == "JsonCo Bulk"
    assert "JsonCo Bulk" in data["row_html"]  # rendered table row for in-place insert

    # same name again (case-insensitive) -> dedup guard
    r = client.post("/companies", data={"name": "jsonco bulk"}, headers=JSON_ACCEPT)
    assert r.status_code == 409
    assert r.json()["status"] == "duplicate"


def test_bulk_parse_endpoint_flags_already_tracked():
    client.post("/companies", data={"name": "Tracked Bulk Co"}, headers=JSON_ACCEPT)
    r = client.post(
        "/companies/bulk/parse",
        data={"text": "Tracked Bulk Co= https://tracked-bulk.example\nFresh Co= https://fresh.example"},
        headers=JSON_ACCEPT,
    )
    assert r.status_code == 200
    rows = r.json()["rows"]
    assert rows[0]["error"] == "already_exists"
    assert rows[1]["error"] == ""
