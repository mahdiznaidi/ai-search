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


def test_load_settings_resolves_relative_paths_against_env_file(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("OPENROUTER_API_KEY=k\nDB_PATH=data/x.db\nLETTERS_DIR=data/letters\n",
                        encoding="utf-8")
    other_cwd = tmp_path / "elsewhere"
    other_cwd.mkdir()
    monkeypatch.chdir(other_cwd)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("DB_PATH", raising=False)
    monkeypatch.delenv("LETTERS_DIR", raising=False)

    s = load_settings(env=None, env_file=str(env_file))
    assert s.db_path == tmp_path / "data" / "x.db"
    assert s.letters_dir == tmp_path / "data" / "letters"


def test_load_settings_keeps_absolute_paths_untouched(tmp_path):
    abs_db = tmp_path / "abs.db"
    s = load_settings({"OPENROUTER_API_KEY": "k", "DB_PATH": str(abs_db)},
                      env_file=str(tmp_path / "somewhere" / ".env"))
    assert s.db_path == abs_db
