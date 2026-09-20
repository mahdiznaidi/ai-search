import json

import pytest

from stagefinder import cli, db


def test_campaign_add_term_and_list(tmp_path, capsys, monkeypatch):
    dbp = tmp_path / "s.db"
    env = {"OPENROUTER_API_KEY": "k", "DB_PATH": str(dbp), "LETTERS_DIR": str(tmp_path)}
    monkeypatch.setattr(cli, "_env", lambda: env)

    assert cli.main(["campaign", "add", "--name", "DS", "--intent", "stage data", "--country", "fr",
                     "--job-type", "internship", "--letter-language", "en", "--hours-old", "168"]) == 0
    out = capsys.readouterr().out
    assert "campagne 1" in out

    assert cli.main(["campaign", "term", "1", "--kind", "keyword", "stage data", "intern ml"]) == 0
    assert cli.main(["campaign", "term", "1", "--kind", "location", "Paris"]) == 0
    assert cli.main(["campaign", "list"]) == 0
    out = capsys.readouterr().out
    assert "DS" in out and "FR" in out and "en" in out

    conn = db.connect(dbp)
    assert conn.execute("SELECT COUNT(*) FROM campaign_terms").fetchone()[0] == 3


def test_run_invokes_pipeline(tmp_path, monkeypatch):
    dbp = tmp_path / "s.db"
    cv = tmp_path / "cv.pdf"; cv.write_bytes(b"x")
    env = {"OPENROUTER_API_KEY": "k", "DB_PATH": str(dbp), "LETTERS_DIR": str(tmp_path),
           "CV_PATH": str(cv)}
    monkeypatch.setattr(cli, "_env", lambda: env)
    cli.main(["campaign", "add", "--name", "DS", "--intent", "i", "--country", "FR"])
    called = {}

    def fake_run(conn, settings, campaign_id, sources, llm, run_id=None, limit=20, **kw):
        called.update(campaign_id=campaign_id, sources=[s.name for s in sources],
                      run_id=run_id, limit=limit)
        conn.execute("INSERT OR REPLACE INTO runs(id,status,campaign_id) VALUES (7,'done',?)", (campaign_id,))
        conn.commit()
        return 7
    monkeypatch.setattr(cli.pipeline, "run_campaign", fake_run)

    assert cli.main(["run", "--campaign", "1", "--no-jobteaser", "--limit", "9"]) == 0
    assert called == {"campaign_id": 1, "sources": ["jobspy"], "run_id": None, "limit": 9}
    assert cli.main(["run", "--campaign", "1", "--resume", "7"]) == 0
    assert called["run_id"] == 7 and called["sources"] == ["jobspy", "jobteaser"]


def test_jobs_retry_failed_enrich(tmp_path, capsys, monkeypatch):
    from stagefinder.dedup import upsert_job
    from stagefinder.models import RawJob

    dbp = tmp_path / "s.db"
    env = {"OPENROUTER_API_KEY": "k", "DB_PATH": str(dbp), "LETTERS_DIR": str(tmp_path)}
    monkeypatch.setattr(cli, "_env", lambda: env)
    cli.main(["campaign", "add", "--name", "DS", "--intent", "i", "--country", "FR"])

    conn = db.connect(dbp)
    r = upsert_job(conn, RawJob("indeed", "k1", "Stage DS", "Co", "Paris", "https://x", "d"), 1, "k", "l")
    conn.execute("UPDATE jobs SET status='failed_enrich', failure_reason='fetch: 500' WHERE id=?",
                 (r.job_id,))
    conn.commit()
    conn.close()

    assert cli.main(["jobs", "retry-failed", "--stage", "enrich"]) == 0
    out = capsys.readouterr().out
    assert "1" in out

    conn = db.connect(dbp)
    row = conn.execute("SELECT status, failure_reason FROM jobs WHERE id=?", (r.job_id,)).fetchone()
    assert row["status"] == "discovered" and row["failure_reason"] is None


def test_letters_regenerate_accepts_extended_language_choices(tmp_path, monkeypatch):
    parser = cli.build_parser()
    a = parser.parse_args(["letters", "regenerate", "1", "--campaign", "1", "--language", "de"])
    assert a.language == "de"
    with pytest.raises(SystemExit):
        parser.parse_args(["letters", "regenerate", "1", "--campaign", "1", "--language", "xx"])


def test_letters_regenerate_without_cv_fails_explicitly(tmp_path, capsys, monkeypatch):
    dbp = tmp_path / "s.db"
    env = {"OPENROUTER_API_KEY": "k", "DB_PATH": str(dbp), "LETTERS_DIR": str(tmp_path)}
    monkeypatch.setattr(cli, "_env", lambda: env)
    cli.main(["campaign", "add", "--name", "DS", "--intent", "i", "--country", "FR"])

    assert cli.main(["letters", "regenerate", "1", "--campaign", "1"]) == 1
    assert "CV_PATH" in capsys.readouterr().err


def test_campaign_suggest_prints_and_apply_adds_terms(tmp_path, monkeypatch, capsys):
    from stagefinder import db
    dbp = tmp_path / "s.db"
    env = {"OPENROUTER_API_KEY": "k", "DB_PATH": str(dbp), "LETTERS_DIR": str(tmp_path)}
    monkeypatch.setattr(cli, "_env", lambda: env)
    cli.main(["campaign", "add", "--name", "DE", "--intent", "ingénieur IA junior", "--country", "DE"])
    capsys.readouterr()

    class FakeLLM:
        model = "fake"
        def __init__(self):
            self.calls = 0
        def chat_json(self, system, user, **kw):
            self.calls += 1
            return {"keyword": ["KI Junior"], "exclude": ["Senior"], "location": ["Berlin"]}

    monkeypatch.setattr(cli, "_llm", lambda settings: FakeLLM())
    assert cli.main(["campaign", "suggest", "1"]) == 0
    out = capsys.readouterr().out
    assert "KI Junior" in out and "Berlin" in out

    conn = db.connect(dbp)
    assert conn.execute("SELECT COUNT(*) FROM campaign_terms").fetchone()[0] == 0

    assert cli.main(["campaign", "suggest", "1", "--apply"]) == 0
    assert conn.execute("SELECT COUNT(*) FROM campaign_terms WHERE origin='ai'").fetchone()[0] == 3

    capsys.readouterr()
    assert cli.main(["campaign", "terms", "1"]) == 0
    out = capsys.readouterr().out
    assert "KI Junior" in out and "ai" in out and "actif" in out


def test_campaign_term_toggle_disables_term(tmp_path, monkeypatch):
    from stagefinder import db
    dbp = tmp_path / "s.db"
    env = {"OPENROUTER_API_KEY": "k", "DB_PATH": str(dbp), "LETTERS_DIR": str(tmp_path)}
    monkeypatch.setattr(cli, "_env", lambda: env)
    cli.main(["campaign", "add", "--name", "DE", "--intent", "x", "--country", "DE"])
    cli.main(["campaign", "term", "1", "--kind", "keyword", "abc"])
    conn = db.connect(dbp)
    tid = conn.execute("SELECT id FROM campaign_terms").fetchone()[0]

    assert cli.main(["campaign", "term-toggle", str(tid), "--disable"]) == 0
    assert conn.execute("SELECT enabled FROM campaign_terms WHERE id=?", (tid,)).fetchone()[0] == 0

    assert cli.main(["campaign", "term-toggle", str(tid), "--enable"]) == 0
    assert conn.execute("SELECT enabled FROM campaign_terms WHERE id=?", (tid,)).fetchone()[0] == 1
