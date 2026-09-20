from __future__ import annotations

import hashlib
import re
import sqlite3
from dataclasses import dataclass

from rapidfuzz import fuzz

from stagefinder.db import now
from stagefinder.models import RawJob

FUZZY_THRESHOLD = 90

# Bruit RH par langue : (h/f), F/H, (m/w/d), M/F/D, (H/M)…
# Parenthesized forms are safe to strip broadly ("(h/f)", "(m/w/d)"); unparenthesized
# forms are restricted to the classic gender pairs so legitimate text like "Trading F/X"
# is not mistaken for gender noise.
_GENDER_PAREN = re.compile(r"\(\s*[hfmwdx]\s*(?:/\s*[hfmwdx]\s*){1,2}\)", re.I)
_GENDER_PAIR = re.compile(
    r"\b(?:h\s*/\s*f|f\s*/\s*h|m\s*/\s*f|f\s*/\s*m|m\s*/\s*w|w\s*/\s*m)"
    r"(?:\s*/\s*[dx])?\b", re.I)
_DURATION = re.compile(r"\b\d+\s*(?:mois|months?|monate|meses)\b", re.I)
_YEAR = re.compile(r"\b20\d\d\b")
_PUNCT = re.compile(r"[^\w\s]", re.UNICODE)
_WS = re.compile(r"\s+")

# Synonymes canonisés vers un seul token
_SYNONYMS = {
    "stagiaire": "stage", "internship": "stage", "intern": "stage",
    "praktikum": "stage", "praktikant": "stage", "praktikantin": "stage",
    "werkstudent": "stage", "werkstudentin": "stage", "pasantia": "stage",
    "tirocinio": "stage", "estagio": "stage",
}
# "via indeed" is noise anywhere in the string; legal-form suffixes (sas, gmbh, group,
# france…) are only noise when they are the LAST token(s) — stripping them anywhere
# would mangle real names that start with, or contain, those same short words (e.g.
# "NV Energy", "France Télévisions").
_VIA = re.compile(r"\bvia indeed\b", re.I)
_LEGAL_SUFFIX = re.compile(
    r"\s+(?:sas|sa|sarl|sasu|eurl|gmbh|ag|ltd|limited|inc|llc|plc|bv|nv|srl|spa|"
    r"group|groupe|france)\s*$", re.I)


def normalize_title(t: str) -> str:
    t = (t or "").lower()
    t = _GENDER_PAREN.sub(" ", t)
    t = _GENDER_PAIR.sub(" ", t)
    t = _DURATION.sub(" ", t)
    t = _YEAR.sub(" ", t)
    t = _PUNCT.sub(" ", t)
    toks = [_SYNONYMS.get(w, w) for w in _WS.split(t) if w]
    return " ".join(sorted(set(toks)))


def normalize_company(c: str) -> str:
    c = (c or "").lower()
    c = _VIA.sub(" ", c)
    while True:
        stripped = _LEGAL_SUFFIX.sub("", c)
        if stripped == c:
            break
        c = stripped
    c = _PUNCT.sub(" ", c)
    return _WS.sub(" ", c).strip()


def fingerprint(company: str, title: str) -> str:
    key = f"{normalize_company(company)}|{normalize_title(title)}"
    return hashlib.sha1(key.encode("utf-8")).hexdigest()


_ID_PATTERNS = {
    "indeed": [re.compile(r"[?&]v?jk=([0-9a-f]+)", re.I)],
    "linkedin": [re.compile(r"/jobs/view/(?:[^/?#]*?-)?(\d{6,})")],
    "jobteaser": [re.compile(r"/job-offers/([^/?#]+)")],
}


def extract_external_id(source: str, url: str, fallback: str | None) -> str | None:
    for pat in _ID_PATTERNS.get(source, []):
        m = pat.search(url or "")
        if m:
            return m.group(1)
    if fallback:
        return fallback.split("-", 1)[1] if re.match(r"^(in|li|gd|zr)-", fallback) else fallback
    return None


@dataclass
class UpsertResult:
    job_id: int
    created: bool
    duplicate_of: int | None


def _find_canonical(conn: sqlite3.Connection, fp: str, company_norm: str,
                    title_norm: str) -> int | None:
    row = conn.execute(
        "SELECT id FROM jobs WHERE fingerprint=? AND duplicate_of IS NULL ORDER BY id LIMIT 1",
        (fp,)).fetchone()
    if row:
        return row["id"]
    for cand in conn.execute(
            "SELECT id, title FROM jobs WHERE company_norm=? AND duplicate_of IS NULL",
            (company_norm,)):
        if fuzz.token_set_ratio(title_norm, normalize_title(cand["title"])) >= FUZZY_THRESHOLD:
            return cand["id"]
    return None


def _link(conn: sqlite3.Connection, job_id: int, campaign_id: int, kw: str, loc: str) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO job_campaigns(job_id,campaign_id,matched_keyword,matched_location) "
        "VALUES (?,?,?,?)", (job_id, campaign_id, kw, loc))


def upsert_job(conn: sqlite3.Connection, raw: RawJob, campaign_id: int,
               matched_keyword: str, matched_location: str) -> UpsertResult:
    fp = fingerprint(raw.company, raw.title)
    company_norm = normalize_company(raw.company)
    title_norm = normalize_title(raw.title)
    ext = extract_external_id(raw.source, raw.url, raw.external_id) or f"fp:{fp}"
    ts = now()

    existing = conn.execute(
        "SELECT id, duplicate_of FROM jobs WHERE source=? AND external_id=?",
        (raw.source, ext)).fetchone()
    if existing:
        conn.execute("UPDATE jobs SET last_seen_at=?, seen_count=seen_count+1 WHERE id=?",
                     (ts, existing["id"]))
        _link(conn, existing["id"], campaign_id, matched_keyword, matched_location)
        if existing["duplicate_of"]:
            conn.execute("UPDATE jobs SET last_seen_at=?, seen_count=seen_count+1 WHERE id=?",
                         (ts, existing["duplicate_of"]))
            _link(conn, existing["duplicate_of"], campaign_id, matched_keyword, matched_location)
        conn.commit()
        return UpsertResult(existing["id"], False, existing["duplicate_of"])

    canonical = _find_canonical(conn, fp, company_norm, title_norm)
    status = "duplicate" if canonical else "discovered"
    cur = conn.execute(
        "INSERT INTO jobs(source,external_id,fingerprint,company_norm,title,company,location,"
        "url,description,posted_at,status,duplicate_of,first_seen_at,last_seen_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (raw.source, ext, fp, company_norm, raw.title, raw.company, raw.location, raw.url,
         raw.description, raw.posted_at, status, canonical, ts, ts))
    job_id = cur.lastrowid
    _link(conn, job_id, campaign_id, matched_keyword, matched_location)
    if canonical:
        conn.execute("UPDATE jobs SET last_seen_at=?, seen_count=seen_count+1 WHERE id=?",
                     (ts, canonical))
        _link(conn, canonical, campaign_id, matched_keyword, matched_location)
    conn.commit()
    return UpsertResult(job_id, True, canonical)
