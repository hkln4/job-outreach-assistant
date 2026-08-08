from fastapi.testclient import TestClient

from app.db import SessionLocal, init_db
from app.main import app
from app.models import CV


def setup_module():
    init_db()
    # the onboarding gate (main.py) redirects everywhere until a CV exists
    db = SessionLocal()
    try:
        if db.query(CV).first() is None:
            db.add(CV(name="Test Web CV", file_path="data/cv_variants/test_web.pdf"))
            db.commit()
    finally:
        db.close()


client = TestClient(app)


def test_dashboard_loads():
    r = client.get("/")
    assert r.status_code == 200
    assert "Dashboard" in r.text


def test_add_and_list_company():
    r = client.post(
        "/companies",
        data={"name": "Acme Robotics", "website_url": "https://acme.example", "notes": "found via LinkedIn"},
        follow_redirects=True,
    )
    assert r.status_code == 200
    assert "Acme Robotics" in r.text


def test_update_company_state():
    client.post("/companies", data={"name": "TempCo", "website_url": "", "notes": ""})
    list_page = client.get("/companies")
    assert "TempCo" in list_page.text
    # state select renders APPLICABLE selected by default per model default
    assert "APPLICABLE" in list_page.text
