"""Matching directory entries to companies the owner already tracks: catch
legal names and sister domains, but never a mere shared word."""
import pytest

from app.companies.dedup import CompanyIndex, site_label
from app.db import SessionLocal, init_db
from app.models import Company


def setup_module():
    init_db()


@pytest.fixture
def index():
    db = SessionLocal()
    names = [
        ("Dedup BeeVision", "https://www.beevision-dd.com.tr"),
        ("Insider One", "https://useinsider-dd.com"),
        ("Fonet Bilgi Tek", "https://www.fonetbt-dd.com/tr/"),
        ("Güven Future", "https://www.guvenfuture-dd.com"),
        ("Plann", "https://plann-dd.space"),
        ("Ratio Energy / Ratio Technologies", "https://www.ratiotech-dd.com.tr"),
    ]
    rows = [Company(name=n, website_url=u) for n, u in names]
    db.add_all(rows)
    db.commit()
    try:
        yield CompanyIndex(db)
    finally:
        for row in rows:
            db.delete(row)
        db.commit()
        db.close()


def test_site_label_ignores_tld_and_subdomain():
    assert site_label("https://www.beevision.com.tr") == site_label("http://beevision.ai") == "beevision"
    assert site_label("en.arpanet.tr") == "arpanet"


@pytest.mark.parametrize(
    "name,url,expected",
    [
        ("3DTİM ELEKTRONİK A.Ş.", "http://www.beevision-dd.ai", "Dedup BeeVision"),  # sister domain
        ("INSIDER ONE (SOSYO PLUS BİLGİ A.Ş.)", "http://www.insiderone.example", "Insider One"),  # legal name
        ("FONET BİLGİ TEKNOLOJİLERİ A.Ş.", None, "Fonet Bilgi Tek"),  # last word abbreviated
        ("RATIO TECHNOLOGIES A.Ş.", None, "Ratio Energy / Ratio Technologies"),  # either alias
    ],
)
def test_loose_matches(index, name, url, expected):
    assert index.find(name, url).name == expected


@pytest.mark.parametrize(
    "name,url",
    [
        ("CYBERWİSE SİBER GÜVENLİK A.Ş.", "https://cyberwise.example"),  # shares a word, not a name
        ("PLANNET UYDU A.Ş.", "https://plannet.example"),  # one-word name must match a whole word
        ("Insider Two Ltd.", None),
    ],
)
def test_no_false_matches(index, name, url):
    assert index.find(name, url) is None


def test_brand_name_from_screening_counts(index):
    assert index.find("XYZ ELEKTRONİK A.Ş.", None, brand_name="Insider One").name == "Insider One"


def test_strict_mode_is_exact_only(index):
    assert index.find("Insider One", "https://other.example", loose=False).name == "Insider One"
    assert index.find("INSIDER ONE (SOSYO PLUS)", "http://www.beevision-dd.ai", loose=False) is None
