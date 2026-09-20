import logging
from pathlib import Path

import pandas as pd

from stagefinder.sources.jobspy_src import INDEED_COUNTRIES, JobSpySource, rows_to_rawjobs
from stagefinder.sources.jobteaser import JobTeaserSource, parse_jobteaser

FIX = Path(__file__).parent / "fixtures"


def test_rows_to_rawjobs_maps_columns():
    df = pd.DataFrame([{
        "id": "in-abc", "site": "indeed", "job_url": "https://fr.indeed.com/viewjob?jk=abc",
        "title": "Stage DS", "company": "Criteo", "location": "Paris, FR",
        "date_posted": pd.Timestamp("2026-09-18"), "description": "desc",
    }, {
        "id": "li-123", "site": "linkedin", "job_url": "https://www.linkedin.com/jobs/view/123",
        "title": "Intern", "company": None, "location": None, "date_posted": None,
        "description": None,
    }, {
        "id": "test-na", "site": "test", "job_url": "https://example.com",
        "title": "Test", "company": pd.NA, "location": pd.NaT, "date_posted": None,
        "description": float("nan"),
    }])
    jobs = rows_to_rawjobs(df)
    assert jobs[0].source == "indeed" and jobs[0].external_id == "in-abc"
    assert jobs[0].posted_at == "2026-09-18"
    assert jobs[1].company == "" and jobs[1].description == "" and jobs[1].posted_at is None
    assert jobs[2].company == "" and jobs[2].location == "" and jobs[2].description == ""


def test_jobspy_supports_uses_indeed_map():
    s = JobSpySource(scrape=lambda **kw: pd.DataFrame())
    assert s.supports("FR") and s.supports("de")
    assert s.supports("TN")  # LinkedIn couvre tout : la source reste utilisable
    assert "FR" in INDEED_COUNTRIES and INDEED_COUNTRIES["FR"] == "france"


def test_jobspy_search_drops_indeed_for_unsupported_country():
    seen = {}

    def scrape(**kw):
        seen.update(kw)
        return pd.DataFrame()

    JobSpySource(scrape=scrape).search("ml", "Tunis", "TN", "internship", 168, False, 5)
    assert seen["site_name"] == ["linkedin"]
    JobSpySource(scrape=scrape).search("ml", "Paris", "FR", "internship", 168, False, 5)
    assert seen["site_name"] == ["indeed", "linkedin"]
    assert seen["country_indeed"] == "france" and seen["job_type"] == "internship"
    assert seen["hours_old"] == 168 and seen["results_wanted"] == 5


def test_jobspy_search_swallows_scraper_error_as_empty(caplog):
    def scrape(**kw):
        raise RuntimeError("boom")
    with caplog.at_level(logging.WARNING, logger="stagefinder.sources.jobspy_src"):
        assert JobSpySource(scrape=scrape).search("a", "b", "FR", None, None, False, 5) == []
    assert any("boom" in record.message for record in caplog.records)


def test_parse_jobteaser_fixture():
    html = (FIX / "jobteaser.html").read_text(encoding="utf-8")
    jobs = parse_jobteaser(html, "Paris")
    assert len(jobs) == 2
    assert jobs[0].source == "jobteaser"
    assert jobs[0].url == "https://www.jobteaser.com/fr/job-offers/1001-stage-data-scientist"
    assert jobs[0].external_id == "1001-stage-data-scientist"
    assert jobs[1].company == "Doctolib" and jobs[1].location == "Paris"


def test_jobteaser_supports_europe_only():
    s = JobTeaserSource(get=lambda *a, **k: None)
    assert s.supports("FR") and s.supports("DE") and not s.supports("US")


def test_parse_jobteaser_external_id_excludes_query_fragment():
    html = '<div class="job-offer-card"><a href="/fr/job-offers/77-x?ref=h#top"><h3 class="job-offer-card__title">T</h3></a></div>'
    jobs = parse_jobteaser(html, "Paris")
    assert len(jobs) == 1
    assert jobs[0].external_id == "77-x"
