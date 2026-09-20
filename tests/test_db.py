from stagefinder import db
from stagefinder.config import load_settings


def test_migrate_creates_tables_and_is_idempotent(tmp_db):
    conn = db.connect(tmp_db)
    db.migrate(conn)
    db.migrate(conn)  # second appel sans erreur
    tables = {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"campaigns", "campaign_terms", "jobs", "job_campaigns", "scores",
            "facts", "letters", "applications", "runs"} <= tables


def test_foreign_keys_enforced(tmp_db):
    conn = db.connect(tmp_db)
    db.migrate(conn)
    import sqlite3, pytest
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO campaign_terms(campaign_id, term, kind, origin) "
                     "VALUES (999, 'x', 'keyword', 'user')")


def test_load_settings_from_mapping(tmp_path):
    s = load_settings({
        "OPENROUTER_API_KEY": "k", "LLM_MODEL": "m", "MAX_LLM_CALLS_PER_RUN": "7",
        "DB_PATH": str(tmp_path / "x.db"), "LETTERS_DIR": str(tmp_path / "l"),
    })
    assert s.openrouter_api_key == "k"
    assert s.max_llm_calls_per_run == 7
    assert s.cv_path is None


def test_load_settings_requires_api_key():
    import pytest
    with pytest.raises(ValueError):
        load_settings({})
