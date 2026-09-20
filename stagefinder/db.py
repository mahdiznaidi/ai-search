from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS campaigns (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  intent TEXT NOT NULL,
  country TEXT NOT NULL,
  job_type TEXT,
  is_remote INTEGER NOT NULL DEFAULT 0,
  hours_old INTEGER,
  letter_language TEXT NOT NULL DEFAULT 'fr',
  score_threshold INTEGER NOT NULL DEFAULT 70,
  created_at TEXT, last_run_at TEXT
);
CREATE TABLE IF NOT EXISTS campaign_terms (
  id INTEGER PRIMARY KEY,
  campaign_id INTEGER NOT NULL REFERENCES campaigns(id),
  term TEXT NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN ('keyword','must_have','exclude','location')),
  origin TEXT NOT NULL CHECK (origin IN ('ai','user')),
  enabled INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS jobs (
  id INTEGER PRIMARY KEY,
  source TEXT NOT NULL, external_id TEXT NOT NULL,
  fingerprint TEXT NOT NULL,
  company_norm TEXT NOT NULL,
  title TEXT, company TEXT, location TEXT, url TEXT,
  description TEXT,
  full_description TEXT,
  enriched TEXT,
  language TEXT,
  posted_at TEXT,
  status TEXT NOT NULL DEFAULT 'discovered',
  failure_reason TEXT,
  duplicate_of INTEGER REFERENCES jobs(id),
  first_seen_at TEXT, last_seen_at TEXT,
  seen_count INTEGER NOT NULL DEFAULT 1,
  UNIQUE(source, external_id)
);
CREATE INDEX IF NOT EXISTS idx_jobs_fingerprint ON jobs(fingerprint);
CREATE INDEX IF NOT EXISTS idx_jobs_company_norm ON jobs(company_norm);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
CREATE TABLE IF NOT EXISTS job_campaigns (
  job_id INTEGER NOT NULL REFERENCES jobs(id),
  campaign_id INTEGER NOT NULL REFERENCES campaigns(id),
  matched_keyword TEXT, matched_location TEXT,
  PRIMARY KEY (job_id, campaign_id)
);
CREATE TABLE IF NOT EXISTS scores (
  job_id INTEGER NOT NULL REFERENCES jobs(id),
  campaign_id INTEGER NOT NULL REFERENCES campaigns(id),
  score INTEGER, reasons TEXT,
  model TEXT, scored_at TEXT,
  PRIMARY KEY (job_id, campaign_id)
);
CREATE TABLE IF NOT EXISTS facts (
  id INTEGER PRIMARY KEY,
  cv_hash TEXT NOT NULL,
  kind TEXT NOT NULL,
  label TEXT, detail TEXT,
  tech TEXT,
  metric TEXT, period TEXT
);
CREATE INDEX IF NOT EXISTS idx_facts_hash ON facts(cv_hash);
CREATE TABLE IF NOT EXISTS letters (
  id INTEGER PRIMARY KEY,
  job_id INTEGER NOT NULL REFERENCES jobs(id),
  campaign_id INTEGER NOT NULL REFERENCES campaigns(id),
  version INTEGER NOT NULL DEFAULT 1,
  language TEXT NOT NULL,
  angle TEXT, body TEXT, file_path TEXT, warnings TEXT,
  model TEXT, created_at TEXT,
  UNIQUE(job_id, campaign_id, version)
);
CREATE TABLE IF NOT EXISTS applications (
  job_id INTEGER PRIMARY KEY REFERENCES jobs(id),
  letter_id INTEGER REFERENCES letters(id),
  applied_at TEXT, channel TEXT,
  outcome TEXT, notes TEXT
);
CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY,
  campaign_id INTEGER REFERENCES campaigns(id),
  status TEXT NOT NULL,
  stage TEXT,
  counters TEXT,
  llm_calls INTEGER NOT NULL DEFAULT 0,
  started_at TEXT, finished_at TEXT,
  error TEXT
);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def migrate(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()
