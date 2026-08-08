from app.web.files import safe_component, safe_uploaded_filename


def test_safe_component_strips_slashes():
    """Regression: a CV name like 'ML/AI resume' must not produce a nested,
    non-existent directory when turned into a filename."""
    assert "/" not in safe_component("ML/AI resume")
    assert safe_component("ML/AI resume") == "ML_AI_resume"


def test_safe_component_handles_empty_and_odd_input():
    assert safe_component("   ") == "file"
    assert "/" not in safe_component("../../etc/passwd")


def test_safe_uploaded_filename_drops_directories():
    assert safe_uploaded_filename("../../etc/passwd.pdf") == "passwd.pdf"
    assert safe_uploaded_filename("my cv (final).pdf") == "my_cv_final.pdf"
