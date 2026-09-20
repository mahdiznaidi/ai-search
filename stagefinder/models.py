from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass

from stagefinder.db import now


@dataclass
class RawJob:
    source: str
    external_id: str | None
    title: str
    company: str
    location: str
    url: str
    description: str = ""
    posted_at: str | None = None


@dataclass
class Campaign:
    id: int
    name: str
    intent: str
    country: str
    job_type: str | None
    is_remote: bool
    hours_old: int | None
    letter_language: str
    score_threshold: int


@dataclass
class Fact:
    id: int
    kind: str
    label: str
    detail: str
    tech: list[str]
    metric: str
    period: str


def create_campaign(conn: sqlite3.Connection, name: str, intent: str, country: str,
                    job_type: str | None = None, is_remote: bool = False,
                    hours_old: int | None = None, letter_language: str = "fr",
                    score_threshold: int = 70) -> int:
    cur = conn.execute(
        "INSERT INTO campaigns(name,intent,country,job_type,is_remote,hours_old,"
        "letter_language,score_threshold,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (name, intent, country.upper(), job_type, int(is_remote), hours_old,
         letter_language, score_threshold, now()))
    conn.commit()
    return cur.lastrowid


def load_campaign(conn: sqlite3.Connection, campaign_id: int) -> Campaign:
    r = conn.execute("SELECT * FROM campaigns WHERE id=?", (campaign_id,)).fetchone()
    if r is None:
        raise KeyError(f"campagne {campaign_id} introuvable")
    return Campaign(r["id"], r["name"], r["intent"], r["country"], r["job_type"],
                    bool(r["is_remote"]), r["hours_old"], r["letter_language"],
                    r["score_threshold"])


def add_terms(conn: sqlite3.Connection, campaign_id: int, kind: str,
              terms: list[str], origin: str = "user") -> None:
    conn.executemany(
        "INSERT INTO campaign_terms(campaign_id,term,kind,origin) VALUES (?,?,?,?)",
        [(campaign_id, t.strip(), kind, origin) for t in terms if t.strip()])
    conn.commit()


def campaign_terms(conn: sqlite3.Connection, campaign_id: int, kind: str) -> list[str]:
    rows = conn.execute(
        "SELECT term FROM campaign_terms WHERE campaign_id=? AND kind=? AND enabled=1 "
        "ORDER BY id", (campaign_id, kind)).fetchall()
    return [r["term"] for r in rows]


def load_enriched(row: sqlite3.Row) -> dict:
    """Charge le JSON `enriched` d'une offre avec des valeurs par défaut sûres."""
    defaults = {"missions": "", "requirements": [], "tech_stack": [], "team_context": "",
               "text_source": ""}
    raw = row["enriched"] if row is not None else None
    try:
        data = json.loads(raw) if raw else {}
    except (TypeError, ValueError, json.JSONDecodeError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    out = dict(defaults)
    out["missions"] = str(data.get("missions", "") or "")
    reqs = data.get("requirements")
    out["requirements"] = [str(x) for x in reqs] if isinstance(reqs, list) else []
    tech = data.get("tech_stack")
    out["tech_stack"] = [str(x) for x in tech] if isinstance(tech, list) else []
    out["team_context"] = str(data.get("team_context", "") or "")
    out["text_source"] = str(data.get("text_source", "") or "")
    return out


def row_to_fact(r: sqlite3.Row) -> Fact:
    return Fact(r["id"], r["kind"], r["label"] or "", r["detail"] or "",
                json.loads(r["tech"] or "[]"), r["metric"] or "", r["period"] or "")
