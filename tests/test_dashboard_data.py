import json

import pytest

from stagefinder import db
from stagefinder.dashboard import data as D
from stagefinder.dedup import upsert_job
from stagefinder.models import RawJob, create_campaign


@pytest.fixture
def conn(tmp_db):
    c = db.connect(tmp_db)
    db.migrate(c)
    return c


def _job(conn, cid, ext, title, company="Co", loc="Paris"):
    r = upsert_job(conn, RawJob("indeed", ext, title, company, loc,
                                f"https://x/{ext}", "d"), cid, "k", "l")
    return r.job_id


def test_list_campaigns_empty_then_one(conn):
    assert D.list_campaigns(conn) == []
    create_campaign(conn, "DS", "intent", "FR")
    rows = D.list_campaigns(conn)
    assert len(rows) == 1 and rows[0]["name"] == "DS" and rows[0]["country"] == "FR"


def test_list_campaign_terms(conn):
    cid = create_campaign(conn, "DS", "intent", "FR")
    conn.execute("INSERT INTO campaign_terms(campaign_id,term,kind,origin) VALUES (?,?,?,?)",
                (cid, "stage data", "keyword", "user"))
    conn.commit()
    rows = D.list_campaign_terms(conn, cid)
    assert rows[0]["term"] == "stage data" and rows[0]["enabled"] == 1


def test_list_jobs_filters_and_sorts_by_score(conn):
    cid = create_campaign(conn, "DS", "intent", "FR")
    a = _job(conn, cid, "a", "Stage A")
    b = _job(conn, cid, "b", "Stage B")
    conn.execute("UPDATE jobs SET status='scored' WHERE id IN (?,?)", (a, b))
    conn.execute("INSERT INTO scores(job_id,campaign_id,score,reasons,model,scored_at) "
                "VALUES (?,?,?,?,?,?)", (a, cid, 60, "[]", "m", "t"))
    conn.execute("INSERT INTO scores(job_id,campaign_id,score,reasons,model,scored_at) "
                "VALUES (?,?,?,?,?,?)", (b, cid, 90, "[]", "m", "t"))
    conn.commit()
    rows = D.list_jobs(conn, campaign_id=cid)
    assert [r["id"] for r in rows] == [b, a]
    assert D.list_jobs(conn, campaign_id=cid, status="scored") == rows
    assert D.list_jobs(conn, campaign_id=999) == []


def test_mark_duplicate_updates_status_and_seen_count(conn):
    cid = create_campaign(conn, "DS", "intent", "FR")
    a = _job(conn, cid, "a", "Stage A")
    b = _job(conn, cid, "b", "Stage A bis")
    D.mark_duplicate(conn, b, a)
    row = conn.execute("SELECT status, duplicate_of FROM jobs WHERE id=?", (b,)).fetchone()
    assert row["status"] == "duplicate" and row["duplicate_of"] == a
    assert conn.execute("SELECT seen_count FROM jobs WHERE id=?", (a,)).fetchone()[0] == 2


def test_list_letters_parses_warnings_json(conn):
    cid = create_campaign(conn, "DS", "intent", "FR")
    jid = _job(conn, cid, "a", "Stage A")
    conn.execute(
        "INSERT INTO letters(job_id,campaign_id,version,language,angle,body,file_path,warnings,"
        "model,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (jid, cid, 1, "fr", "{}", "corps", "x.docx", json.dumps(["longueur"]), "m", "t"))
    conn.commit()
    rows = D.list_letters(conn, campaign_id=cid)
    assert rows[0]["warnings"] == ["longueur"] and rows[0]["title"] == "Stage A"


def test_upsert_application_then_list(conn):
    cid = create_campaign(conn, "DS", "intent", "FR")
    jid = _job(conn, cid, "a", "Stage A")
    D.upsert_application(conn, jid, None, "2026-09-20", "email", "pending", "relancer dans 1 sem")
    rows = D.list_applications(conn)
    assert rows[0]["job_id"] == jid and rows[0]["outcome"] == "pending"
    D.upsert_application(conn, jid, None, "2026-09-20", "email", "interview", "")
    rows = D.list_applications(conn)
    assert len(rows) == 1 and rows[0]["outcome"] == "interview"


def test_runs_listing_and_latest(conn):
    cid = create_campaign(conn, "DS", "intent", "FR")
    conn.execute("INSERT INTO runs(campaign_id,status,started_at) VALUES (?,?,?)", (cid, "done", db.now()))
    conn.execute("INSERT INTO runs(campaign_id,status,started_at) VALUES (?,?,?)", (cid, "failed", db.now()))
    conn.commit()
    assert len(D.list_runs(conn, cid)) == 2
    assert D.latest_run(conn, cid)["status"] == "failed"
    assert D.latest_run(conn, 999) is None


def test_list_jobs_excludes_duplicates_by_default(conn):
    cid = create_campaign(conn, "DS", "intent", "FR")
    a = _job(conn, cid, "a", "Stage A")
    b = _job(conn, cid, "b", "Stage A bis")
    D.mark_duplicate(conn, b, a)
    rows = D.list_jobs(conn, campaign_id=cid)
    assert [r["id"] for r in rows] == [a]


def test_list_jobs_accepts_a_list_of_statuses(conn):
    cid = create_campaign(conn, "DS", "intent", "FR")
    a = _job(conn, cid, "a", "Stage A")
    b = _job(conn, cid, "b", "Stage B")
    e = _job(conn, cid, "e", "Stage E")
    conn.execute("UPDATE jobs SET status='scored' WHERE id=?", (a,))
    conn.execute("UPDATE jobs SET status='skipped' WHERE id=?", (b,))
    conn.execute("UPDATE jobs SET status='letter_ready' WHERE id=?", (e,))
    conn.commit()
    rows = D.list_jobs(conn, campaign_id=cid, status=["scored", "skipped"])
    assert {r["id"] for r in rows} == {a, b}


def test_list_jobs_exposes_score_reasons(conn):
    cid = create_campaign(conn, "DS", "intent", "FR")
    a = _job(conn, cid, "a", "Stage A")
    conn.execute("UPDATE jobs SET status='scored' WHERE id=?", (a,))
    conn.execute("INSERT INTO scores(job_id,campaign_id,score,reasons,model,scored_at) "
                "VALUES (?,?,?,?,?,?)", (a, cid, 80, '["Python solide", "LangChain"]', "m", "t"))
    conn.commit()
    rows = D.list_jobs(conn, campaign_id=cid)
    assert rows[0]["reasons"] == ["Python solide", "LangChain"]


def test_delete_campaign_removes_campaign_terms_scores_and_letters_but_keeps_jobs(conn):
    import json as _json

    cid = create_campaign(conn, "DS", "intent", "FR")
    jid = _job(conn, cid, "a", "Stage A")
    conn.execute("INSERT INTO campaign_terms(campaign_id,term,kind,origin) VALUES (?,?,?,?)",
                (cid, "stage data", "keyword", "user"))
    conn.execute("INSERT INTO scores(job_id,campaign_id,score,reasons,model,scored_at) "
                "VALUES (?,?,?,?,?,?)", (jid, cid, 80, "[]", "m", "t"))
    conn.execute(
        "INSERT INTO letters(job_id,campaign_id,version,language,angle,body,file_path,warnings,"
        "model,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (jid, cid, 1, "fr", "{}", "corps", "x.docx", "[]", "m", "t"))
    conn.commit()

    D.delete_campaign(conn, cid)

    assert D.list_campaigns(conn) == []
    assert conn.execute("SELECT COUNT(*) FROM campaign_terms WHERE campaign_id=?", (cid,)).fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM scores WHERE campaign_id=?", (cid,)).fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM letters WHERE campaign_id=?", (cid,)).fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM job_campaigns WHERE campaign_id=?", (cid,)).fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM jobs WHERE id=?", (jid,)).fetchone()[0] == 1
