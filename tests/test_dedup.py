import pytest

from stagefinder import db
from stagefinder.dedup import (extract_external_id, fingerprint, normalize_company,
                               normalize_title, upsert_job)
from stagefinder.models import RawJob, create_campaign


@pytest.fixture
def conn(tmp_db):
    c = db.connect(tmp_db)
    db.migrate(c)
    return c


@pytest.fixture
def cid(conn):
    return create_campaign(conn, "c", "i", "FR")


@pytest.mark.parametrize("a,b", [
    ("Stage Data Scientist H/F - 6 mois", "Data Scientist Intern (M/F)"),
    ("Stagiaire Data Scientist (F/H) 2026", "Stage – Data Scientist"),
    ("Praktikum Machine Learning (m/w/d)", "Machine Learning Internship"),
])
def test_normalize_title_converges(a, b):
    assert normalize_title(a) == normalize_title(b)


def test_normalize_title_keeps_distinguishing_words():
    assert normalize_title("Stage Data Scientist") != normalize_title("Stage Data Engineer")
    assert normalize_title("Stage Data Scientist") != normalize_title("CDI Data Scientist")


def test_normalize_title_gender_regex_does_not_overmatch():
    assert normalize_title("Stage Trading F/X") != normalize_title("Stage Trading")


@pytest.mark.parametrize("a,b", [
    ("Criteo SA", "Criteo"), ("BUTAGAZ SAS", "Butagaz"),
    ("Siemens GmbH", "siemens"), ("Doctolib via Indeed", "Doctolib"),
])
def test_normalize_company(a, b):
    assert normalize_company(a) == normalize_company(b)


def test_fingerprint_is_stable_and_ignores_location():
    assert fingerprint("Criteo SA", "Stage Data Scientist H/F") == \
        fingerprint("Criteo", "Data Scientist Intern (M/F)")


def test_normalize_company_keeps_leading_short_legal_like_tokens():
    assert normalize_company("NV Energy") == "nv energy"
    assert normalize_company("Renault Group France") == "renault"


@pytest.mark.parametrize("source,url,fb,expected", [
    ("indeed", "https://fr.indeed.com/viewjob?jk=a1b2c3d4e5f6&from=serp", None, "a1b2c3d4e5f6"),
    ("indeed", "https://fr.indeed.com/rc/clk?jk=ffff0000&tk=x", None, "ffff0000"),
    ("linkedin", "https://www.linkedin.com/jobs/view/3912345678/?ref=x", None, "3912345678"),
    ("linkedin", "https://fr.linkedin.com/jobs/view/data-scientist-at-x-3912345678", None, "3912345678"),
    ("indeed", "https://weird/url", "in-zzz", "zzz"),
    ("jobteaser", "https://www.jobteaser.com/fr/job-offers/12345-stage-data", None, "12345-stage-data"),
    ("indeed", "https://weird/url", None, None),
])
def test_extract_external_id(source, url, fb, expected):
    assert extract_external_id(source, url, fb) == expected


def _raw(source="indeed", ext="k1", title="Stage Data Scientist H/F",
         company="Criteo SA", url="https://fr.indeed.com/viewjob?jk=k1", loc="Paris (75)"):
    return RawJob(source, ext, title, company, loc, url, "desc")


def test_layer1_same_source_id_increments_seen(conn, cid):
    r1 = upsert_job(conn, _raw(), cid, "kw", "Paris")
    r2 = upsert_job(conn, _raw(), cid, "kw2", "Paris")
    assert r1.created and not r2.created and r1.job_id == r2.job_id
    row = conn.execute("SELECT seen_count, status FROM jobs WHERE id=?", (r1.job_id,)).fetchone()
    assert row["seen_count"] == 2 and row["status"] == "discovered"
    assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1


def test_layer2_cross_platform_same_offer_is_duplicate(conn, cid):
    a = upsert_job(conn, _raw(), cid, "kw", "Paris")
    b = upsert_job(conn, _raw(source="linkedin", ext="99", title="Data Scientist Intern (M/F)",
                        company="Criteo", url="https://www.linkedin.com/jobs/view/99",
                        loc="Paris, Île-de-France, France"), cid, "kw", "Paris")
    assert b.created and b.duplicate_of == a.job_id
    st = conn.execute("SELECT status FROM jobs WHERE id=?", (b.job_id,)).fetchone()["status"]
    assert st == "duplicate"


def test_layer3_fuzzy_same_company_city_suffix(conn, cid):
    a = upsert_job(conn, _raw(), cid, "kw", "Paris")
    b = upsert_job(conn, _raw(source="linkedin", ext="77",
                        title="Stage Data Scientist H/F - Paris",
                        url="https://www.linkedin.com/jobs/view/77"), cid, "kw", "Paris")
    assert b.duplicate_of == a.job_id


def test_layer1_resighting_duplicate_increments_canonical_seen_count(conn, cid):
    a = upsert_job(conn, _raw(), cid, "kw", "Paris")
    dup_raw = _raw(source="linkedin", ext="99", title="Data Scientist Intern (M/F)",
                    company="Criteo", url="https://www.linkedin.com/jobs/view/99",
                    loc="Paris, Île-de-France, France")
    upsert_job(conn, dup_raw, cid, "kw", "Paris")
    upsert_job(conn, dup_raw, cid, "kw", "Paris")
    seen = conn.execute("SELECT seen_count FROM jobs WHERE id=?", (a.job_id,)).fetchone()["seen_count"]
    assert seen == 3


def test_distinct_roles_same_company_not_merged(conn, cid):
    a = upsert_job(conn, _raw(), cid, "kw", "Paris")
    b = upsert_job(conn, _raw(ext="k2", title="Stage Data Engineer H/F",
                        url="https://fr.indeed.com/viewjob?jk=k2"), cid, "kw", "Paris")
    assert b.duplicate_of is None and b.job_id != a.job_id


def test_job_linked_to_campaign(conn, cid):
    r = upsert_job(conn, _raw(), cid, "stage data", "Paris")
    row = conn.execute("SELECT * FROM job_campaigns WHERE job_id=?", (r.job_id,)).fetchone()
    assert row["campaign_id"] == cid and row["matched_keyword"] == "stage data"


def test_no_external_id_falls_back_to_fingerprint(conn, cid):
    r = upsert_job(conn, _raw(ext=None, url="https://weird"), cid, "k", "l")
    ext = conn.execute("SELECT external_id FROM jobs WHERE id=?", (r.job_id,)).fetchone()[0]
    assert ext.startswith("fp:")
