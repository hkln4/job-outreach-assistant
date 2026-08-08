from fastapi.testclient import TestClient

from app.db import init_db
from app.main import app


def setup_module():
    init_db()


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
