from __future__ import annotations

import json
import sqlite3

from stagefinder.db import now


def _rows(cur: sqlite3.Cursor) -> list[dict]:
    return [dict(r) for r in cur.fetchall()]


def list_campaigns(conn: sqlite3.Connection) -> list[dict]:
    return _rows(conn.execute("SELECT * FROM campaigns ORDER BY id"))


def list_campaign_terms(conn: sqlite3.Connection, campaign_id: int) -> list[dict]:
    return _rows(conn.execute(
        "SELECT * FROM campaign_terms WHERE campaign_id=? ORDER BY kind, id", (campaign_id,)))


def list_jobs(conn: sqlite3.Connection, campaign_id: int | None = None,
             status: str | None = None) -> list[dict]:
    sql = ("SELECT j.id, j.title, j.company, j.location, j.status, j.seen_count, "
          "j.duplicate_of, j.url, s.score "
          "FROM jobs j LEFT JOIN scores s ON s.job_id=j.id "
          "AND s.campaign_id = COALESCE(:cid, s.campaign_id) ")
    where, params = [], {}
    if campaign_id is not None:
        sql += "JOIN job_campaigns jc ON jc.job_id=j.id "
        where.append("jc.campaign_id=:cid")
        params["cid"] = campaign_id
    else:
        params["cid"] = None
    if status is not None:
        where.append("j.status=:status")
        params["status"] = status
    if where:
        sql += "WHERE " + " AND ".join(where) + " "
    sql += "ORDER BY (s.score IS NULL), s.score DESC, j.id"
    return _rows(conn.execute(sql, params))


def mark_duplicate(conn: sqlite3.Connection, job_id: int, canonical_id: int) -> None:
    conn.execute("UPDATE jobs SET status='duplicate', duplicate_of=? WHERE id=?",
                (canonical_id, job_id))
    conn.execute("UPDATE jobs SET seen_count=seen_count+1, last_seen_at=? WHERE id=?",
                (now(), canonical_id))
    conn.commit()


def list_letters(conn: sqlite3.Connection, campaign_id: int | None = None) -> list[dict]:
    sql = ("SELECT l.id, l.job_id, l.campaign_id, l.version, l.language, l.file_path, "
          "l.warnings, l.created_at, j.company, j.title "
          "FROM letters l JOIN jobs j ON j.id=l.job_id ")
    params: dict = {}
    if campaign_id is not None:
        sql += "WHERE l.campaign_id=:cid "
        params["cid"] = campaign_id
    sql += "ORDER BY l.id DESC"
    rows = _rows(conn.execute(sql, params))
    for r in rows:
        r["warnings"] = json.loads(r["warnings"] or "[]")
    return rows


def list_applications(conn: sqlite3.Connection) -> list[dict]:
    return _rows(conn.execute(
        "SELECT a.*, j.title, j.company FROM applications a JOIN jobs j ON j.id=a.job_id "
        "ORDER BY a.applied_at DESC"))


def upsert_application(conn: sqlite3.Connection, job_id: int, letter_id: int | None,
                       applied_at: str | None, channel: str | None, outcome: str | None,
                       notes: str | None) -> None:
    conn.execute(
        "INSERT INTO applications(job_id,letter_id,applied_at,channel,outcome,notes) "
        "VALUES (?,?,?,?,?,?) ON CONFLICT(job_id) DO UPDATE SET "
        "letter_id=excluded.letter_id, applied_at=excluded.applied_at, "
        "channel=excluded.channel, outcome=excluded.outcome, notes=excluded.notes",
        (job_id, letter_id, applied_at, channel, outcome, notes))
    conn.commit()


def list_runs(conn: sqlite3.Connection, campaign_id: int) -> list[dict]:
    return _rows(conn.execute(
        "SELECT * FROM runs WHERE campaign_id=? ORDER BY id DESC", (campaign_id,)))


def latest_run(conn: sqlite3.Connection, campaign_id: int) -> dict | None:
    r = conn.execute("SELECT * FROM runs WHERE campaign_id=? ORDER BY id DESC LIMIT 1",
                     (campaign_id,)).fetchone()
    return dict(r) if r else None
