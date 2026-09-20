import json
from pathlib import Path

import pytest

from stagefinder import db, letters as L
from stagefinder.dedup import upsert_job
from stagefinder.models import Fact, RawJob, create_campaign, load_campaign

FACTS = [
    Fact(1, "project", "Agent de veille", "Collecte et synthèse d'articles", ["LangChain", "FAISS"],
         "10 000 articles traités", "2025"),
    Fact(2, "skill", "Python et SQL", "", ["Python", "SQL"], "", ""),
]
ANGLE = {"language": "fr", "company_hooks": ["gouvernance des données"],
         "pairs": [{"requirement": "qualité des données", "fact_id": 1,
                    "why": "dédoublonnage de 10 000 articles"}],
         "gaps": ["Talend"], "opening_angle": "données industrielles"}

GOOD = ("Madame, Monsieur,\n\nVotre offre de Stage Data Scientist chez Criteo porte sur la gouvernance "
        "des données. " + "Le nettoyage de 10 000 articles scientifiques avec LangChain et FAISS m'a "
        "confronté aux mêmes problèmes de qualité. " * 15 +
        "Je serais heureux d'en discuter.\n\nCordialement")


@pytest.fixture
def conn(tmp_db):
    c = db.connect(tmp_db); db.migrate(c); return c


def _seed(conn, language="fr"):
    cid = create_campaign(conn, "c", "stage data", "FR", letter_language=language)
    r = upsert_job(conn, RawJob("indeed", "k1", "Stage Data Scientist", "Criteo", "Paris",
                                "https://x", "d"), cid, "k", "l")
    conn.execute("UPDATE jobs SET status='scored', language='fr', full_description='offre', "
                 "enriched=? WHERE id=?",
                 (json.dumps({"missions": "gouvernance", "requirements": ["Python"],
                              "tech_stack": ["Python", "Talend"], "team_context": ""}), r.job_id))
    conn.execute("INSERT INTO scores(job_id,campaign_id,score,reasons,model,scored_at) "
                 "VALUES (?,?,?,?,?,?)", (r.job_id, cid, 88, '["ML"]', "m", "t"))
    conn.commit()
    return load_campaign(conn, cid), r.job_id


def _job(conn, jid):
    return conn.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone()


def test_validate_passes_good_letter(conn):
    _, jid = _seed(conn)
    assert L.validate_letter(GOOD, _job(conn, jid), FACTS[:1], ["Python", "Talend"], "fr") == []


def test_validate_flags_tech_not_in_cited_facts(conn):
    _, jid = _seed(conn)
    body = GOOD.replace("FAISS", "Talend")
    issues = L.validate_letter(body, _job(conn, jid), FACTS[:1], ["Python", "Talend"], "fr")
    assert any("Talend" in i for i in issues)


def test_validate_flags_blacklist_language_length_company(conn):
    _, jid = _seed(conn)
    short_en = "I am a highly motivated candidate for this role at Acme."
    issues = L.validate_letter(short_en, _job(conn, jid), FACTS[:1], [], "fr")
    joined = " ".join(issues).lower()
    assert "langue" in joined and "mots" in joined and "criteo" in joined
    fr_black = GOOD.replace("Je serais heureux", "Je suis très motivé et rigoureux, je serais heureux")
    assert any("formule" in i for i in L.validate_letter(fr_black, _job(conn, jid), FACTS[:1], [], "fr"))


def test_validate_flags_unknown_number(conn):
    _, jid = _seed(conn)
    body = GOOD.replace("10 000 articles", "10 000 articles et 250 modèles")
    assert any("250" in i for i in L.validate_letter(body, _job(conn, jid), FACTS[:1], [], "fr"))


def test_validate_flags_unresolved_placeholder(conn):
    _, jid = _seed(conn)
    body = GOOD.replace("Cordialement", "Cordialement,\n[Votre Nom]")
    issues = L.validate_letter(body, _job(conn, jid), FACTS[:1], [], "fr")
    assert any("placeholder" in i for i in issues)


def test_generate_for_job_success(conn, fake_llm, tmp_path):
    camp, jid = _seed(conn)
    fake_llm.responses = [json.dumps(ANGLE), GOOD]
    lid = L.generate_for_job(conn, fake_llm, jid, camp, FACTS, tmp_path)
    row = conn.execute("SELECT * FROM letters WHERE id=?", (lid,)).fetchone()
    assert row["version"] == 1 and row["language"] == "fr" and row["body"] == GOOD
    assert json.loads(row["warnings"]) == []
    assert Path(row["file_path"]).exists()
    assert _job(conn, jid)["status"] == "letter_ready"
    assert fake_llm.calls == 2
    # la rédaction ne reçoit que les faits cités
    assert "[1]" in fake_llm.prompts[1][1] and "[2]" not in fake_llm.prompts[1][1]


def test_generate_retries_once_then_keeps_warnings(conn, fake_llm, tmp_path):
    camp, jid = _seed(conn)
    bad = GOOD.replace("FAISS", "Talend")
    fake_llm.responses = [json.dumps(ANGLE), bad, bad]
    lid = L.generate_for_job(conn, fake_llm, jid, camp, FACTS, tmp_path)
    w = json.loads(conn.execute("SELECT warnings FROM letters WHERE id=?", (lid,)).fetchone()[0])
    assert w and fake_llm.calls == 3
    assert "Talend" in fake_llm.prompts[2][1]  # la reprise reçoit les écarts
    assert _job(conn, jid)["status"] == "letter_ready"


def test_generate_versions_and_language_override(conn, fake_llm, tmp_path):
    camp, jid = _seed(conn)
    fake_llm.responses = [json.dumps(ANGLE), GOOD]
    L.generate_for_job(conn, fake_llm, jid, camp, FACTS, tmp_path)
    en = ("Dear Hiring Manager,\n\nYour Stage Data Scientist offer at Criteo focuses on data governance. "
          + "Cleaning 10 000 scientific articles with LangChain and FAISS exposed me to the same data "
            "quality issues. " * 15 + "Best regards")
    fake_llm.responses = [json.dumps({**ANGLE, "language": "en"}), en]
    lid2 = L.generate_for_job(conn, fake_llm, jid, camp, FACTS, tmp_path,
                              instruction="plus court", language="en")
    row = conn.execute("SELECT version, language FROM letters WHERE id=?", (lid2,)).fetchone()
    assert row["version"] == 2 and row["language"] == "en"
    assert "plus court" in fake_llm.prompts[-1][1]


def test_auto_language_follows_job(conn, fake_llm, tmp_path):
    camp, jid = _seed(conn, language="auto")
    fake_llm.responses = [json.dumps(ANGLE), GOOD]
    lid = L.generate_for_job(conn, fake_llm, jid, camp, FACTS, tmp_path)
    assert conn.execute("SELECT language FROM letters WHERE id=?", (lid,)).fetchone()[0] == "fr"


def test_auto_language_fallback_is_reported_as_warning(conn, fake_llm, tmp_path):
    camp, jid = _seed(conn, language="auto")
    conn.execute("UPDATE jobs SET language=NULL WHERE id=?", (jid,)); conn.commit()
    en = ("Dear Hiring Manager,\n\nYour Stage Data Scientist offer at Criteo focuses on data "
          "governance. " + "Cleaning 10 000 scientific articles with LangChain and FAISS exposed "
          "me to the same data quality issues. " * 15 + "Best regards")
    fake_llm.responses = [json.dumps({**ANGLE, "language": "en"}), en]
    lid = L.generate_for_job(conn, fake_llm, jid, camp, FACTS, tmp_path)
    row = conn.execute("SELECT language, warnings FROM letters WHERE id=?", (lid,)).fetchone()
    assert row["language"] == "en"
    assert any("anglais utilisé par défaut" in w for w in json.loads(row["warnings"]))


def test_generate_pending_counts_and_llm_failure(conn, fake_llm, tmp_path):
    from stagefinder.llm import LLMError
    camp, jid = _seed(conn)

    def boom():
        raise LLMError("x")
    fake_llm.responses = [boom]
    assert L.generate_pending(conn, fake_llm, camp, FACTS, tmp_path) == {"letters": 0, "failed": 1}
    assert _job(conn, jid)["status"] == "failed_letter"


def test_write_letter_strips_llm_chrome(conn, fake_llm, tmp_path):
    _, jid = _seed(conn)
    fake_llm.responses = ["Voici la lettre :\n\n```\nMadame, Monsieur,\n\n**Corps**\n```"]
    body = L.write_letter(fake_llm, _job(conn, jid), ANGLE, FACTS[:1], "fr")
    assert body.startswith("Madame, Monsieur,")
    assert "**" not in body and "```" not in body


def test_render_docx(tmp_path):
    p = tmp_path / "l.docx"
    L.render_docx("Ligne 1\n\nLigne 2", p)
    from docx import Document
    assert [x.text for x in Document(p).paragraphs] == ["Ligne 1", "Ligne 2"]


def test_build_angle_rejects_non_dict_response(conn, fake_llm, tmp_path):
    from stagefinder.llm import LLMError
    camp, jid = _seed(conn)
    fake_llm.responses = ["[1]"]
    assert L.generate_pending(conn, fake_llm, camp, FACTS, tmp_path) == {"letters": 0, "failed": 1}
    assert _job(conn, jid)["status"] == "failed_letter"


def test_validate_flags_number_that_is_substring_of_real_one(conn):
    _, jid = _seed(conn)
    body = GOOD.replace("10 000 articles", "1 000 articles")
    issues = L.validate_letter(body, _job(conn, jid), FACTS[:1], [], "fr")
    assert any("1 000" in i for i in issues)


def test_validate_flags_cpp_tech_with_symbol_boundary(conn):
    _, jid = _seed(conn)
    body = "Bonjour, je code en C++ depuis plusieurs années chez vous."
    issues = L.validate_letter(body, _job(conn, jid), FACTS[:1], ["C++"], "fr")
    assert any("C++" in i for i in issues)


def test_validate_does_not_flag_cpp_when_cited(conn):
    _, jid = _seed(conn)
    cpp_fact = Fact(3, "skill", "Dev C++", "", ["C++"], "", "")
    body = "Bonjour, je code en C++ depuis plusieurs années chez vous."
    issues = L.validate_letter(body, _job(conn, jid), [cpp_fact], ["C++"], "fr")
    assert not any("C++" in i for i in issues)


def test_validate_flags_tech_leaked_from_non_cited_facts(conn):
    _, jid = _seed(conn)
    body = GOOD + " Nous avons aussi utilisé Kubernetes en production."
    all_facts = FACTS  # FACTS[1] has no tech overlap ("Python", "SQL"); use one with Kubernetes
    k8s_fact = Fact(3, "skill", "DevOps", "", ["Kubernetes"], "", "")
    all_facts = FACTS + [k8s_fact]
    issues = L.validate_letter(body, _job(conn, jid), FACTS[:1], ["Python", "Talend"], "fr",
                               all_facts)
    assert any("Kubernetes" in i for i in issues)


def test_validate_does_not_flag_tech_when_no_all_facts_given(conn):
    _, jid = _seed(conn)
    body = GOOD + " Nous avons aussi utilisé Kubernetes en production."
    issues = L.validate_letter(body, _job(conn, jid), FACTS[:1], ["Python", "Talend"], "fr")
    assert not any("Kubernetes" in i for i in issues)


def test_validate_flags_missing_company_name(conn):
    _, jid = _seed(conn)
    conn.execute("UPDATE jobs SET company='' WHERE id=?", (jid,))
    conn.commit()
    issues = L.validate_letter(GOOD, _job(conn, jid), FACTS[:1], ["Python", "Talend"], "fr")
    assert any("entreprise" in i for i in issues)


def test_generate_continues_when_pairs_items_are_not_dicts(conn, fake_llm, tmp_path):
    camp, jid = _seed(conn)
    bad_angle = {**ANGLE, "pairs": ["1", 2]}
    fake_llm.responses = [json.dumps(bad_angle), GOOD]
    counters = L.generate_pending(conn, fake_llm, camp, FACTS, tmp_path)
    assert counters == {"letters": 1, "failed": 0}
    row = conn.execute("SELECT warnings FROM letters WHERE job_id=? AND campaign_id=?",
                       (jid, camp.id)).fetchone()
    assert any("3 premiers faits utilisés" in w for w in json.loads(row["warnings"]))


def test_generate_fails_when_pairs_is_not_a_list(conn, fake_llm, tmp_path):
    camp, jid = _seed(conn)
    bad_angle = {**ANGLE, "pairs": {"a": 1}}
    fake_llm.responses = [json.dumps(bad_angle)]
    assert L.generate_pending(conn, fake_llm, camp, FACTS, tmp_path) == {"letters": 0, "failed": 1}
    assert _job(conn, jid)["status"] == "failed_letter"


def test_generate_docx_filename_prefixed_by_job_id(conn, fake_llm, tmp_path):
    camp, jid = _seed(conn)
    fake_llm.responses = [json.dumps(ANGLE), GOOD]
    lid = L.generate_for_job(conn, fake_llm, jid, camp, FACTS, tmp_path)
    row = conn.execute("SELECT file_path FROM letters WHERE id=?", (lid,)).fetchone()
    assert Path(row["file_path"]).name.startswith(f"{jid}_")
