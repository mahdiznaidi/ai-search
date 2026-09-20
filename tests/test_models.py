import pytest

from stagefinder import db
from stagefinder.models import (add_terms, campaign_terms, create_campaign,
                                load_campaign, load_enriched)


@pytest.fixture
def conn(tmp_db):
    c = db.connect(tmp_db)
    db.migrate(c)
    return c


def test_create_and_load_campaign(conn):
    cid = create_campaign(conn, "DS FR", "stage data science", "FR",
                          job_type="internship", hours_old=168)
    c = load_campaign(conn, cid)
    assert c.name == "DS FR" and c.country == "FR"
    assert c.letter_language == "fr" and c.score_threshold == 70
    assert c.is_remote is False


def test_load_missing_campaign_raises(conn):
    with pytest.raises(KeyError):
        load_campaign(conn, 42)


def test_terms_filtered_by_kind_and_enabled(conn):
    cid = create_campaign(conn, "x", "y", "FR")
    add_terms(conn, cid, "keyword", ["stage data", "intern ml"])
    add_terms(conn, cid, "exclude", ["senior"])
    conn.execute("UPDATE campaign_terms SET enabled=0 WHERE term='intern ml'")
    assert campaign_terms(conn, cid, "keyword") == ["stage data"]
    assert campaign_terms(conn, cid, "exclude") == ["senior"]
    assert campaign_terms(conn, cid, "location") == []


class _Row(dict):
    def __getitem__(self, k):
        return dict.get(self, k)


def test_load_enriched_defaults_on_malformed_json():
    row = _Row(enriched="{not valid json")
    e = load_enriched(row)
    assert e == {"missions": "", "requirements": [], "tech_stack": [], "team_context": "",
                 "text_source": ""}


def test_load_enriched_defaults_on_missing_keys():
    row = _Row(enriched='{"missions": "x"}')
    e = load_enriched(row)
    assert e["missions"] == "x"
    assert e["requirements"] == [] and e["tech_stack"] == [] and e["team_context"] == ""
    assert e["text_source"] == ""


def test_load_enriched_reads_all_fields():
    import json as _json
    row = _Row(enriched=_json.dumps({"missions": "m", "requirements": ["r"],
                                     "tech_stack": ["Python"], "team_context": "c",
                                     "text_source": "page"}))
    e = load_enriched(row)
    assert e == {"missions": "m", "requirements": ["r"], "tech_stack": ["Python"],
                "team_context": "c", "text_source": "page"}
