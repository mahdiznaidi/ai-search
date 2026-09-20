import pytest

from stagefinder import db
from stagefinder.models import (add_terms, campaign_terms, create_campaign,
                                load_campaign)


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
