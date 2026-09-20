import json

import pytest

from stagefinder import db, pipeline as PL
from stagefinder.config import Settings
from stagefinder.models import RawJob, add_terms, create_campaign, load_campaign

PAGE = "<html><body>" + "Stage data science, missions : analyse de données avec Python. " * 10 + "</body></html>"
ENRICHED = json.dumps({"missions": "analyse", "requirements": ["Python"], "tech_stack": ["Python"],
                       "team_context": ""})
FACTS_JSON = json.dumps({"facts": [{"kind": "skill", "label": "Python", "detail": "3 ans",
                                    "tech": ["Python"], "metric": "", "period": ""}]})
LETTER = ("Madame, Monsieur,\n\nVotre offre de Stage Data Scientist chez Criteo m'intéresse. "
          + "Mes trois ans de pratique de Python en analyse de données répondent à votre besoin. " * 18
          + "Cordialement")


class FakeSource:
    name = "fake"

    def __init__(self, jobs):
        self.jobs = jobs
        self.queries = []

    def supports(self, country):
        return True

    def search(self, keyword, location, country, job_type, hours_old, is_remote, limit):
        self.queries.append((keyword, location))
        return list(self.jobs)


@pytest.fixture
def conn(tmp_db):
    c = db.connect(tmp_db); db.migrate(c); return c


@pytest.fixture
def settings(tmp_path):
    cv = tmp_path / "cv.pdf"; cv.write_bytes(b"pdf")
    return Settings("k", "m", 50, cv, tmp_path / "db.sqlite", tmp_path / "letters")


@pytest.fixture(autouse=True)
def _no_pdf(monkeypatch):
    import stagefinder.profile as P
    monkeypatch.setattr(P, "extract_pdf_text", lambda p: "CV")


def _campaign(conn):
    cid = create_campaign(conn, "DS", "stage data science", "FR", score_threshold=70)
    add_terms(conn, cid, "keyword", ["stage data", "stage ml"])
    add_terms(conn, cid, "location", ["Paris", "Lyon"])
    return cid


def _jobs():
    return [RawJob("indeed", "in-a", "Stage Data Scientist", "Criteo", "Paris",
                   "https://fr.indeed.com/viewjob?jk=a", "d"),
            RawJob("linkedin", "li-1", "Data Scientist Intern (M/F)", "Criteo SA", "Paris, FR",
                   "https://www.linkedin.com/jobs/view/1", "d")]


def test_discover_builds_matrix_and_dedups(conn):
    cid = _campaign(conn)
    src = FakeSource(_jobs())
    c = PL.discover(conn, load_campaign(conn, cid), [src], limit=5, workers=2)
    assert sorted(src.queries) == sorted([(k, l) for k in ["stage data", "stage ml"] for l in ["Paris", "Lyon"]])
    assert c["found"] == 8 and c["new"] == 2 and c["duplicates"] == 1
    assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 2


def test_discover_skips_unsupported_source(conn):
    cid = _campaign(conn)
    src = FakeSource(_jobs()); src.supports = lambda c: False
    assert PL.discover(conn, load_campaign(conn, cid), [src])["found"] == 0


def _run(conn, settings, cid, llm, src):
    return PL.run_campaign(conn, settings, cid, [src], llm,
                           fetch=lambda u, timeout=15: PAGE, limit=5)


def test_run_campaign_end_to_end_and_idempotent(conn, settings, fake_llm):
    cid = _campaign(conn)
    src = FakeSource(_jobs())
    fake_llm.responses = [
        FACTS_JSON,                                    # profil
        ENRICHED,                                      # enrich (1 job canonique)
        json.dumps([{"index": 0, "score": 90, "reasons": ["Python"]}]),  # score
        json.dumps({"language": "fr", "company_hooks": [], "gaps": [],   # angle
                    "pairs": [{"requirement": "Python", "fact_id": 1, "why": "3 ans"}],
                    "opening_angle": ""}),
        LETTER,                                        # rédaction
    ]
    run_id = _run(conn, settings, cid, fake_llm, src)
    run = conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
    assert run["status"] == "done" and run["stage"] == "generate"
    counters = json.loads(run["counters"])
    assert counters["new"] == 2 and counters["enriched"] == 1
    assert counters["scored"] == 1 and counters["letters"] == 1
    assert run["llm_calls"] == 5
    assert conn.execute("SELECT COUNT(*) FROM letters").fetchone()[0] == 1
    assert conn.execute("SELECT status FROM jobs WHERE duplicate_of IS NULL").fetchone()[0] == "letter_ready"

    # ---- second run identique : aucune nouveauté, aucun appel LLM, aucune lettre ----
    before = fake_llm.calls
    run2 = _run(conn, settings, cid, fake_llm, src)
    r2 = conn.execute("SELECT * FROM runs WHERE id=?", (run2,)).fetchone()
    assert r2["status"] == "done"
    assert fake_llm.calls == before
    assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM letters").fetchone()[0] == 1
    assert json.loads(r2["counters"])["new"] == 0
    assert conn.execute("SELECT seen_count FROM jobs WHERE external_id='a'").fetchone()[0] > 1


def test_run_campaign_budget_exceeded_marks_failed(conn, settings, fake_llm):
    from stagefinder.llm import BudgetExceeded
    cid = _campaign(conn)

    def boom():
        raise BudgetExceeded("plafond")
    fake_llm.responses = [FACTS_JSON, boom]
    run_id = _run(conn, settings, cid, fake_llm, FakeSource(_jobs()))
    run = conn.execute("SELECT status, error, stage FROM runs WHERE id=?", (run_id,)).fetchone()
    assert run["status"] == "failed" and "plafond" in run["error"] and run["stage"] == "enrich"


def test_run_campaign_llm_fatal_marks_failed(conn, settings, fake_llm):
    from stagefinder.llm import LLMFatal
    cid = _campaign(conn)

    def boom():
        raise LLMFatal("clé invalide")
    fake_llm.responses = [FACTS_JSON, boom]
    run_id = _run(conn, settings, cid, fake_llm, FakeSource(_jobs()))
    run = conn.execute("SELECT status, error, stage FROM runs WHERE id=?", (run_id,)).fetchone()
    assert run["status"] == "failed" and run["error"].startswith("llm:") and run["stage"] == "enrich"
    assert conn.execute("SELECT COUNT(*) FROM jobs WHERE status='failed_enrich'").fetchone()[0] == 0


def test_run_campaign_without_cv_fails_explicitly(conn, settings, fake_llm):
    cid = _campaign(conn)
    s = Settings("k", "m", 50, None, settings.db_path, settings.letters_dir)
    run_id = PL.run_campaign(conn, s, cid, [FakeSource([])], fake_llm)
    run = conn.execute("SELECT status, error FROM runs WHERE id=?", (run_id,)).fetchone()
    assert run["status"] == "failed" and "CV_PATH" in run["error"]


def test_resume_reuses_run_row(conn, settings, fake_llm):
    cid = _campaign(conn)
    fake_llm.responses = [FACTS_JSON]
    rid = PL.run_campaign(conn, settings, cid, [FakeSource([])], fake_llm)
    rid2 = PL.run_campaign(conn, settings, cid, [FakeSource([])], fake_llm, run_id=rid)
    assert rid == rid2
    assert conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 1
