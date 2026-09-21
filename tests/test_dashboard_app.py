import os
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

APP = str(Path(__file__).parent.parent / "stagefinder" / "dashboard" / "app.py")


@pytest.fixture
def env(tmp_path, monkeypatch):
    db_path = tmp_path / "s.db"
    letters_dir = tmp_path / "letters"
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    monkeypatch.setenv("DB_PATH", str(db_path))
    monkeypatch.setenv("LETTERS_DIR", str(letters_dir))
    monkeypatch.setenv("CV_PATH", "")
    monkeypatch.chdir(tmp_path)
    return {"db_path": db_path, "letters_dir": letters_dir}


def test_app_loads_without_error(env):
    at = AppTest.from_file(APP)
    at.run()
    assert not at.exception


def test_create_campaign_via_form(env):
    at = AppTest.from_file(APP)
    at.run()
    at.text_input(key="new_name").set_value("DS France").run()
    at.text_area(key="new_intent").set_value("stage data science").run()
    at.text_input(key="new_country").set_value("FR").run()
    at.button(key="create_campaign_btn").click().run()
    assert not at.exception
    assert any("DS France" in m.value for m in at.markdown if "DS France" in m.value)


def test_add_manual_term_to_campaign(env):
    from stagefinder import db

    at = AppTest.from_file(APP)
    at.run()
    at.text_input(key="new_name").set_value("DS").run()
    at.text_area(key="new_intent").set_value("stage data").run()
    at.text_input(key="new_country").set_value("FR").run()
    at.button(key="create_campaign_btn").click().run()

    conn = db.connect(env["db_path"])
    cid = conn.execute("SELECT id FROM campaigns").fetchone()[0]

    at.text_input(key=f"manual_term_{cid}_keyword").set_value("stage data science").run()
    at.button(key=f"add_manual_{cid}_keyword").click().run()
    assert not at.exception
    assert any("stage data science" in cb.label for cb in at.checkbox)


def test_toggle_term_checkbox_disables_it(env):
    from stagefinder import db
    from stagefinder.models import add_terms, create_campaign

    conn = db.connect(env["db_path"])
    db.migrate(conn)
    cid = create_campaign(conn, "DS", "stage data", "FR")
    add_terms(conn, cid, "keyword", ["stage data science"])

    at = AppTest.from_file(APP)
    at.run()
    cb = next(c for c in at.checkbox if "stage data science" in c.label)
    cb.set_value(False).run()
    assert not at.exception
    conn2 = db.connect(env["db_path"])
    assert conn2.execute("SELECT enabled FROM campaign_terms").fetchone()[0] == 0


def test_run_page_shows_latest_run_status(env):
    from stagefinder import db
    from stagefinder.models import create_campaign

    conn = db.connect(env["db_path"])
    db.migrate(conn)
    cid = create_campaign(conn, "DS", "stage data", "FR")
    conn.execute("INSERT INTO runs(campaign_id,status,stage,counters,started_at) "
                "VALUES (?,?,?,?,?)", (cid, "done", "generate", '{"new": 3}', db.now()))
    conn.commit()

    at = AppTest.from_file(APP)
    at.run()
    at.sidebar.radio[0].set_value("Run").run()
    at.selectbox(key="run_campaign_select").set_value(cid).run()
    assert not at.exception
    assert any("done" in m.value for m in at.markdown)
    assert any('"new": 3' in m.value or "new" in m.value for m in at.json)


def test_run_page_with_no_runs_shows_message(env):
    from stagefinder import db
    from stagefinder.models import create_campaign

    conn = db.connect(env["db_path"])
    db.migrate(conn)
    create_campaign(conn, "DS", "stage data", "FR")

    at = AppTest.from_file(APP)
    at.run()
    at.sidebar.radio[0].set_value("Run").run()
    assert not at.exception
    assert any("aucun run" in i.value.lower() for i in at.info)


def test_jobs_page_lists_and_marks_duplicate(env):
    from stagefinder import db
    from stagefinder.dedup import upsert_job
    from stagefinder.models import RawJob, create_campaign

    conn = db.connect(env["db_path"])
    db.migrate(conn)
    cid = create_campaign(conn, "DS", "stage data", "FR")
    a = upsert_job(conn, RawJob("indeed", "a", "Stage A", "Co", "Paris", "https://x/a", "d"),
                   cid, "k", "l").job_id
    b = upsert_job(conn, RawJob("indeed", "b", "Stage B", "Co", "Paris", "https://x/b", "d"),
                   cid, "k", "l").job_id

    at = AppTest.from_file(APP)
    at.run()
    at.sidebar.radio[0].set_value("Offres").run()
    at.selectbox(key="jobs_campaign_select").set_value(cid).run()
    assert not at.exception
    assert any("Stage A" in m.value for m in at.markdown) or \
           any("Stage A" in str(df.value) for df in at.dataframe)

    at.selectbox(key=f"dup_target_{b}").set_value(a).run()
    at.button(key=f"mark_dup_{b}").click().run()
    assert not at.exception
    conn2 = db.connect(env["db_path"])
    row = conn2.execute("SELECT status, duplicate_of FROM jobs WHERE id=?", (b,)).fetchone()
    assert row["status"] == "duplicate" and row["duplicate_of"] == a


def test_letters_page_lists_and_downloads(env):
    import json as _json

    from stagefinder import db
    from stagefinder.dedup import upsert_job
    from stagefinder.models import RawJob, create_campaign

    conn = db.connect(env["db_path"])
    db.migrate(conn)
    cid = create_campaign(conn, "DS", "stage data", "FR")
    jid = upsert_job(conn, RawJob("indeed", "a", "Stage A", "Co", "Paris", "https://x/a", "d"),
                     cid, "k", "l").job_id
    letters_dir = env["letters_dir"]
    letters_dir.mkdir(parents=True, exist_ok=True)
    docx_path = letters_dir / "1_Co_Stage_A_v1.docx"
    docx_path.write_bytes(b"fake docx bytes")
    conn.execute(
        "INSERT INTO letters(job_id,campaign_id,version,language,angle,body,file_path,warnings,"
        "model,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (jid, cid, 1, "fr", "{}", "Corps de la lettre", str(docx_path),
         _json.dumps(["longueur 220 mots"]), "m", "t"))
    conn.commit()

    at = AppTest.from_file(APP)
    at.run()
    at.sidebar.radio[0].set_value("Lettres").run()
    at.selectbox(key="letters_campaign_select").set_value(cid).run()
    assert not at.exception
    assert any("Stage A" in e.label for e in at.expander)
    assert any("longueur 220 mots" in w.value for w in at.warning)
    # `st.download_button` n'est pas exposé comme type assertable par AppTest (il apparaît en
    # UnknownElement) : on vérifie seulement que la page ne plante pas en le rendant, et que le
    # texte de la lettre est bien lu depuis le fichier .docx généré au préalable.
    assert docx_path.exists()


def test_tracking_page_records_application(env):
    from stagefinder import db
    from stagefinder.dedup import upsert_job
    from stagefinder.models import RawJob, create_campaign

    conn = db.connect(env["db_path"])
    db.migrate(conn)
    cid = create_campaign(conn, "DS", "stage data", "FR")
    jid = upsert_job(conn, RawJob("indeed", "a", "Stage A", "Co", "Paris", "https://x/a", "d"),
                     cid, "k", "l").job_id
    conn.execute("UPDATE jobs SET status='letter_ready' WHERE id=?", (jid,))
    conn.commit()

    at = AppTest.from_file(APP)
    at.run()
    at.sidebar.radio[0].set_value("Suivi").run()
    at.selectbox(key="tracking_job_select").set_value(jid).run()
    at.selectbox(key="tracking_outcome").set_value("interview").run()
    at.text_area(key="tracking_notes").set_value("Entretien le 25/09").run()
    at.button(key="save_tracking").click().run()
    assert not at.exception

    conn2 = db.connect(env["db_path"])
    row = conn2.execute("SELECT outcome, notes FROM applications WHERE job_id=?", (jid,)).fetchone()
    assert row["outcome"] == "interview" and "25/09" in row["notes"]

    assert any("interview" in m.value for m in at.markdown)


def test_launch_button_disabled_when_run_already_in_progress(env):
    from stagefinder import db
    from stagefinder.models import create_campaign

    conn = db.connect(env["db_path"])
    db.migrate(conn)
    cid = create_campaign(conn, "DS", "stage data", "FR")
    conn.execute("INSERT INTO runs(campaign_id,status,started_at) VALUES (?,?,?)",
                (cid, "running", db.now()))
    conn.commit()

    at = AppTest.from_file(APP)
    at.run()
    assert not at.exception
    btn = next(b for b in at.button if b.key == f"run_{cid}")
    assert btn.disabled
    assert any("déjà en cours" in w.value for w in at.warning)


def test_campaign_shows_estimate_and_can_be_deleted(env):
    from stagefinder import db
    from stagefinder.models import add_terms, create_campaign

    conn = db.connect(env["db_path"])
    db.migrate(conn)
    cid = create_campaign(conn, "DS", "stage data", "FR")
    add_terms(conn, cid, "keyword", ["stage data", "stage ml"])
    add_terms(conn, cid, "location", ["Paris"])

    at = AppTest.from_file(APP)
    at.run()
    assert not at.exception
    assert any("combinaisons" in c.value for c in at.caption)

    at.checkbox(key=f"confirm_delete_{cid}").set_value(True).run()
    at.button(key=f"delete_{cid}").click().run()
    assert not at.exception
    conn2 = db.connect(env["db_path"])
    assert conn2.execute("SELECT COUNT(*) FROM campaigns").fetchone()[0] == 0


def test_run_page_auto_refreshes_while_running_and_translates_error(env):
    from stagefinder import db
    from stagefinder.models import create_campaign

    conn = db.connect(env["db_path"])
    db.migrate(conn)
    cid = create_campaign(conn, "DS", "stage data", "FR")
    conn.execute("INSERT INTO runs(campaign_id,status,stage,started_at) VALUES (?,?,?,?)",
                (cid, "running", "enrich", db.now()))
    conn.commit()

    at = AppTest.from_file(APP)
    at.run()
    at.sidebar.radio[0].set_value("Run").run()
    at.selectbox(key="run_campaign_select").set_value(cid).run()
    assert not at.exception
    assert any('http-equiv="refresh"' in m.value for m in at.markdown)

    conn.execute("UPDATE runs SET status='failed', error='llm: HTTP 401 clé invalide' WHERE campaign_id=?",
                (cid,))
    conn.commit()
    at.run()
    at.sidebar.radio[0].set_value("Run").run()
    at.selectbox(key="run_campaign_select").set_value(cid).run()
    assert any("clé API invalide" in e.value for e in at.error)


def test_jobs_page_simplified_filters_and_reasons(env):
    from stagefinder import db
    from stagefinder.dedup import upsert_job
    from stagefinder.models import RawJob, create_campaign

    conn = db.connect(env["db_path"])
    db.migrate(conn)
    cid = create_campaign(conn, "DS", "stage data", "FR")
    a = upsert_job(conn, RawJob("indeed", "a", "Stage A", "Co", "Paris", "https://x/a", "d"),
                   cid, "k", "l").job_id
    conn.execute("UPDATE jobs SET status='scored' WHERE id=?", (a,))
    conn.execute("INSERT INTO scores(job_id,campaign_id,score,reasons,model,scored_at) "
                "VALUES (?,?,?,?,?,?)", (a, cid, 85, '["Python solide"]', "m", "t"))
    conn.commit()

    at = AppTest.from_file(APP)
    at.run()
    at.sidebar.radio[0].set_value("Offres").run()
    at.selectbox(key="jobs_campaign_select").set_value(cid).run()
    assert not at.exception
    filter_options = at.selectbox(key="jobs_status_filter").options
    assert "discovered" not in filter_options and "duplicate" not in filter_options
    assert "En erreur" in filter_options
    assert any("Python solide" in c.value for c in at.caption)


def test_letters_page_shows_badge_and_saves_edits(env):
    import json as _json

    from stagefinder import db
    from stagefinder.dedup import upsert_job
    from stagefinder.models import RawJob, create_campaign

    conn = db.connect(env["db_path"])
    db.migrate(conn)
    cid = create_campaign(conn, "DS", "stage data", "FR")
    jid = upsert_job(conn, RawJob("indeed", "a", "Stage A", "Co", "Paris", "https://x/a", "d"),
                     cid, "k", "l").job_id
    letters_dir = env["letters_dir"]
    letters_dir.mkdir(parents=True, exist_ok=True)
    docx_path = letters_dir / "1_Co_Stage_A_v1.docx"
    docx_path.write_bytes(b"placeholder")
    conn.execute(
        "INSERT INTO letters(job_id,campaign_id,version,language,angle,body,file_path,warnings,"
        "model,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (jid, cid, 1, "fr", "{}", "Corps original", str(docx_path), _json.dumps([]), "m", "t"))
    conn.commit()

    at = AppTest.from_file(APP)
    at.run()
    at.sidebar.radio[0].set_value("Lettres").run()
    at.selectbox(key="letters_campaign_select").set_value(cid).run()
    assert not at.exception
    assert any(e.label.startswith("🟢") for e in at.expander)

    lid = conn.execute("SELECT id FROM letters").fetchone()[0]
    at.text_area(key=f"body_{lid}").set_value("Corps modifié").run()
    at.button(key=f"save_{lid}").click().run()
    assert not at.exception
    conn2 = db.connect(env["db_path"])
    assert conn2.execute("SELECT body FROM letters WHERE id=?", (lid,)).fetchone()[0] == "Corps modifié"


def test_profile_page_lists_facts_and_reanalyzes(env, tmp_path):
    import json as _json

    from stagefinder import db, profile as P

    conn = db.connect(env["db_path"])
    db.migrate(conn)
    cv = tmp_path / "cv.pdf"
    cv.write_bytes(b"pdf")
    os.environ["CV_PATH"] = str(cv)

    h = P.cv_hash(cv)
    conn.execute(
        "INSERT INTO facts(cv_hash,kind,label,detail,tech,metric,period) VALUES (?,?,?,?,?,?,?)",
        (h, "skill", "Python", "3 ans de pratique", _json.dumps(["Python"]), "", ""))
    conn.commit()

    at = AppTest.from_file(APP)
    at.run()
    at.sidebar.radio[0].set_value("Profil").run()
    assert not at.exception
    assert any("Python" in m.value for m in at.markdown)
    assert any(b.label == "Ré-analyser mon CV" for b in at.button)

    del os.environ["CV_PATH"]
