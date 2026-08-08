from app.email_send.gmail_client import validate_email


def test_validate_email_accepts_normal_address():
    assert validate_email("hsnkln4@gmail.com")


def test_validate_email_rejects_malformed():
    assert not validate_email("not-an-email")
    assert not validate_email("missing-domain@")
    assert not validate_email("")
