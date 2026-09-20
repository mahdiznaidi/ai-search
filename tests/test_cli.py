import json

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
