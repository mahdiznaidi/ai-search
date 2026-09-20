import json

import pytest

from stagefinder import db, enrich as E
from stagefinder.dedup import upsert_job
from stagefinder.llm import BudgetExceeded, LLMError, LLMFatal
from stagefinder.models import RawJob, create_campaign

PAGE = """<html><head><title>x</title><script>var a=1</script></head><body>
<nav>menu</nav><h1>Stage Data &amp; IA</h1>
<p>Missions : nettoyage de données, gouvernance. Stack : Python, SQL, Talend.</p>
<p>Vous rejoindrez une équipe data qui structure les référentiels du groupe, définit les règles de qualité et automatise les contrôles sur les flux entrants. Le stage dure six mois et débute en février.</p>
<style>.a{}</style></body></html>"""

ENRICHED = json.dumps({"missions": "nettoyage et gouvernance", "requirements": ["Python", "SQL"],
                       "tech_stack": ["Python", "SQL", "Talend"], "team_context": "équipe data"})


@pytest.fixture
def conn(tmp_db):
    c = db.connect(tmp_db); db.migrate(c); return c


def _seed(conn, n=1, status="discovered"):
    cid = create_campaign(conn, "c", "i", "FR")
    ids = []
    for i in range(n):
        r = upsert_job(conn, RawJob("indeed", f"k{i}", f"Stage {i}", f"Co{i}", "Paris",
                                    f"https://fr.indeed.com/viewjob?jk=k{i}", "d"), cid, "k", "l")
        ids.append(r.job_id)
    if status != "discovered":
        conn.execute("UPDATE jobs SET status=?", (status,)); conn.commit()
    return cid, ids


def test_html_to_text_strips_and_unescapes():
    t = E.html_to_text(PAGE)
    assert "Stage Data & IA" in t and "var a" not in t and "menu" not in t and ".a{}" not in t


def test_detect_language():
    assert E.detect_language("Nous recherchons un stagiaire en science des données pour six mois.") == "fr"
    assert E.detect_language("We are looking for a data science intern for six months.") == "en"
    assert E.detect_language("") == "und"


def test_enrich_job_success(conn, fake_llm):
    _, (jid,) = _seed(conn)
    fake_llm.responses = [ENRICHED]
    assert E.enrich_job(conn, fake_llm, jid, fetch=lambda u, timeout=15: PAGE)
    row = conn.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone()
    assert row["status"] == "enriched" and row["language"] == "fr"
    assert "Stage Data & IA" in row["full_description"]
    assert json.loads(row["enriched"])["tech_stack"] == ["Python", "SQL", "Talend"]


def test_enrich_job_fetch_failure_marks_failed(conn, fake_llm):
    # Description-first (C1): a fetch failure only fails the job when there is no
    # description to fall back on; an empty description makes this the "no fallback" case.
    _, (jid,) = _seed(conn)
    conn.execute("UPDATE jobs SET description='' WHERE id=?", (jid,)); conn.commit()

    def boom(u, timeout=15):
        raise RuntimeError("404")
    assert not E.enrich_job(conn, fake_llm, jid, fetch=boom)
    row = conn.execute("SELECT status, failure_reason FROM jobs WHERE id=?", (jid,)).fetchone()
    assert row["status"] == "failed_enrich" and "404" in row["failure_reason"]
    assert fake_llm.calls == 0


def test_enrich_job_fetch_failure_falls_back_to_description(conn, fake_llm):
    _, (jid,) = _seed(conn)
    conn.execute("UPDATE jobs SET description='Stage data, Python, six mois.' WHERE id=?",
                 (jid,)); conn.commit()
    fake_llm.responses = [ENRICHED]

    def boom(u, timeout=15):
        raise RuntimeError("403")
    assert E.enrich_job(conn, fake_llm, jid, fetch=boom)
    row = conn.execute("SELECT status, enriched FROM jobs WHERE id=?", (jid,)).fetchone()
    assert row["status"] == "enriched"
    assert json.loads(row["enriched"])["text_source"] == "search_description"


def test_enrich_job_long_description_skips_fetch(conn, fake_llm):
    _, (jid,) = _seed(conn)
    long_desc = "Stage data science, missions variées, Python et SQL. " * 12
    assert len(long_desc) >= 500
    conn.execute("UPDATE jobs SET description=? WHERE id=?", (long_desc, jid)); conn.commit()
    fake_llm.responses = [ENRICHED]

    def boom(u, timeout=15):
        raise AssertionError("fetch ne doit jamais être appelé")
    assert E.enrich_job(conn, fake_llm, jid, fetch=boom)
    row = conn.execute("SELECT status, enriched FROM jobs WHERE id=?", (jid,)).fetchone()
    assert row["status"] == "enriched"
    assert json.loads(row["enriched"])["text_source"] == "search_description"


def test_enrich_job_fetch_success_uses_page_source(conn, fake_llm):
    _, (jid,) = _seed(conn)
    conn.execute("UPDATE jobs SET description='courte' WHERE id=?", (jid,)); conn.commit()
    fake_llm.responses = [ENRICHED]
    assert E.enrich_job(conn, fake_llm, jid, fetch=lambda u, timeout=15: PAGE)
    row = conn.execute("SELECT enriched FROM jobs WHERE id=?", (jid,)).fetchone()
    assert json.loads(row["enriched"])["text_source"] == "page"


def test_enrich_pending_only_touches_discovered(conn, fake_llm):
    _, ids = _seed(conn, 3)
    conn.execute("UPDATE jobs SET status='enriched' WHERE id=?", (ids[0],)); conn.commit()
    fake_llm.responses = [ENRICHED, ENRICHED]
    c = E.enrich_pending(conn, fake_llm, fetch=lambda u, timeout=15: PAGE, workers=2)
    assert c == {"enriched": 2, "failed": 0} and fake_llm.calls == 2


def test_enrich_pending_does_not_fetch_long_description_rows(conn, fake_llm):
    cid, (jid,) = _seed(conn)
    long_desc = "Stage data science, missions variées, Python et SQL. " * 12
    conn.execute("UPDATE jobs SET description=? WHERE id=?", (long_desc, jid)); conn.commit()
    fake_llm.responses = [ENRICHED]

    def boom(u, timeout=15):
        raise AssertionError("fetch ne doit jamais être appelé")
    c = E.enrich_pending(conn, fake_llm, fetch=boom)
    assert c == {"enriched": 1, "failed": 0}
    row = conn.execute("SELECT enriched FROM jobs WHERE id=?", (jid,)).fetchone()
    assert json.loads(row["enriched"])["text_source"] == "search_description"


def test_enrich_pending_skips_duplicates(conn, fake_llm):
    _, (jid,) = _seed(conn)
    conn.execute("UPDATE jobs SET status='duplicate'"); conn.commit()
    assert E.enrich_pending(conn, fake_llm, fetch=lambda u, timeout=15: PAGE) == {"enriched": 0, "failed": 0}


def test_enrich_job_non_dict_response_marks_failed(conn, fake_llm):
    _, (jid,) = _seed(conn)
    fake_llm.responses = ["[1, 2]"]
    assert not E.enrich_job(conn, fake_llm, jid, fetch=lambda u, timeout=15: PAGE)
    row = conn.execute("SELECT status, failure_reason FROM jobs WHERE id=?", (jid,)).fetchone()
    assert row["status"] == "failed_enrich" and "llm:" in row["failure_reason"]


def test_enrich_job_llm_error_marks_failed(conn, fake_llm):
    _, (jid,) = _seed(conn)

    def boom():
        raise LLMError("x")
    fake_llm.responses = [boom]
    assert not E.enrich_job(conn, fake_llm, jid, fetch=lambda u, timeout=15: PAGE)
    row = conn.execute("SELECT status, failure_reason FROM jobs WHERE id=?", (jid,)).fetchone()
    assert row["status"] == "failed_enrich" and row["failure_reason"].startswith("llm:")


def test_enrich_pending_propagates_budget_exceeded(conn, fake_llm):
    _, (jid,) = _seed(conn)

    def boom():
        raise BudgetExceeded("plafond")
    fake_llm.responses = [boom]
    with pytest.raises(BudgetExceeded):
        E.enrich_pending(conn, fake_llm, fetch=lambda u, timeout=15: PAGE)
    row = conn.execute("SELECT status FROM jobs WHERE id=?", (jid,)).fetchone()
    assert row["status"] == "discovered"


def test_enrich_pending_propagates_llm_fatal(conn, fake_llm):
    _, (jid,) = _seed(conn)

    def boom():
        raise LLMFatal("clé invalide")
    fake_llm.responses = [boom]
    with pytest.raises(LLMFatal):
        E.enrich_pending(conn, fake_llm, fetch=lambda u, timeout=15: PAGE)
    row = conn.execute("SELECT status FROM jobs WHERE id=?", (jid,)).fetchone()
    assert row["status"] == "discovered"


FR_MISSIONS_ENRICHED = json.dumps({
    "missions": "Vous rejoindrez une équipe data pour structurer les référentiels et automatiser "
                "les contrôles qualité sur les flux entrants de l'entreprise.",
    "requirements": ["Python", "SQL", "gouvernance des données"],
    "tech_stack": ["Python", "SQL"], "team_context": ""})

ENGLISH_CHROME_PAGE = """<html><head><title>Careers</title></head><body>
<nav>Home About Careers Contact Login Sign up</nav>
<header>Welcome to our English company website navigation menu</header>
<p>Apply now. View more jobs. Share this job. Save for later.</p>
</body></html>"""


def test_enrich_job_language_from_extracted_text_not_page_chrome(conn, fake_llm):
    _, (jid,) = _seed(conn)
    conn.execute("UPDATE jobs SET description='courte' WHERE id=?", (jid,)); conn.commit()
    fake_llm.responses = [FR_MISSIONS_ENRICHED]
    assert E.enrich_job(conn, fake_llm, jid, fetch=lambda u, timeout=15: ENGLISH_CHROME_PAGE)
    row = conn.execute("SELECT language FROM jobs WHERE id=?", (jid,)).fetchone()
    assert row["language"] == "fr"


def test_enrich_job_no_usable_text_marks_failed(conn, fake_llm):
    _, (jid,) = _seed(conn)
    conn.execute("UPDATE jobs SET description='' WHERE id=?", (jid,)); conn.commit()
    assert not E.enrich_job(conn, fake_llm, jid, fetch=lambda u, timeout=15: "<html></html>")
    row = conn.execute("SELECT status, failure_reason FROM jobs WHERE id=?", (jid,)).fetchone()
    assert row["status"] == "failed_enrich" and row["failure_reason"] == "aucun texte exploitable"
    assert fake_llm.calls == 0
