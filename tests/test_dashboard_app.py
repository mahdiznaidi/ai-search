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
