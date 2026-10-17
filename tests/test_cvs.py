"""CV variant uploads: two CVs never share (and so never overwrite or delete)
one file, only PDF/DOCX under the size cap get in, and the recipient sees a
clean attachment name rather than the disk name."""
import email
import base64

import pytest
from fastapi.testclient import TestClient

import app.web.routes.cvs as cvs_routes
from app.db import SessionLocal, init_db
from app.email_send.gmail_client import _build_message
from app.main import app
from app.models import CV

client = TestClient(app)


def setup_module():
    init_db()


@pytest.fixture(autouse=True)
def _tmp_cv_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(cvs_routes, "CV_VARIANTS_DIR", str(tmp_path))
    return tmp_path


def _upload(name, filename="cv.pdf", content=b"%PDF-1.4 fake"):
    return client.post(
        "/cvs",
        data={"name": name, "target_role": "", "notes": ""},
        files={"file": (filename, content, "application/pdf")},
        follow_redirects=False,
    )


def _cvs_named(name):
    db = SessionLocal()
    try:
        return db.query(CV).filter(CV.name == name).order_by(CV.id).all()
    finally:
        db.close()


def test_same_display_name_gets_separate_files(_tmp_cv_dir):
    _upload("Twin CV", content=b"%PDF first")
    _upload("Twin CV", content=b"%PDF second")

    first, second = _cvs_named("Twin CV")
    assert first.file_path != second.file_path
    assert open(first.file_path, "rb").read() == b"%PDF first"
    assert open(second.file_path, "rb").read() == b"%PDF second"

    client.post(f"/cvs/{first.id}/delete")
    assert open(second.file_path, "rb").read() == b"%PDF second"


def test_deleting_a_cv_keeps_a_file_another_cv_still_uses(_tmp_cv_dir):
    shared = _tmp_cv_dir / "Legacy_CV.pdf"
    shared.write_bytes(b"%PDF legacy")
    db = SessionLocal()
    try:
        a = CV(name="Legacy A", file_path=str(shared))
        b = CV(name="Legacy B", file_path=str(shared))
        db.add_all([a, b])
        db.commit()
        a_id = a.id
    finally:
        db.close()

    client.post(f"/cvs/{a_id}/delete")
    assert shared.exists()


@pytest.mark.parametrize(
    "filename,content,error",
    [
        ("cv.exe", b"MZ", "type"),
        ("cv", b"%PDF", "type"),
        ("cv.pdf", b"", "empty"),
        ("cv.pdf", b"x" * (cvs_routes.MAX_CV_BYTES + 1), "size"),
    ],
)
def test_rejected_uploads_create_nothing(filename, content, error):
    r = _upload("Rejected CV", filename=filename, content=content)
    assert r.headers["location"] == f"/cvs?error={error}"
    assert _cvs_named("Rejected CV") == []


def test_docx_is_accepted():
    r = _upload("Docx CV", filename="My CV.DOCX", content=b"PK fake docx")
    assert r.headers["location"] == "/cvs"
    (cv,) = _cvs_named("Docx CV")
    assert cv.file_path.endswith(".docx")


def test_attachment_uses_the_clean_name_not_the_disk_name(tmp_path):
    path = tmp_path / "Backend_CV-1a2b3c4d.pdf"
    path.write_bytes(b"%PDF")
    raw = _build_message("hr@acme.example", "Hi", "Body", str(path), "Backend_CV.pdf")["raw"]
    message = email.message_from_bytes(base64.urlsafe_b64decode(raw))
    filenames = [part.get_filename() for part in message.walk() if part.get_filename()]
    assert filenames == ["Backend_CV.pdf"]
