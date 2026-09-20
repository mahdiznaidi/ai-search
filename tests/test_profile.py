import json

import pytest

from stagefinder import db
from stagefinder import profile as P


@pytest.fixture
def conn(tmp_db):
    c = db.connect(tmp_db)
    db.migrate(c)
    return c


FACTS_JSON = json.dumps({"facts": [
    {"kind": "project", "label": "Agent de veille scientifique",
     "detail": "Pipeline de collecte et synthèse d'articles", "tech": ["LangChain", "FAISS"],
     "metric": "10 000 articles traités", "period": "2025"},
    {"kind": "skill", "label": "Python", "detail": "", "tech": ["Python"], "metric": "", "period": ""},
]})


def test_extract_and_cache(conn, fake_llm, tmp_path, monkeypatch):
    cv = tmp_path / "cv.pdf"
    cv.write_bytes(b"%PDF-fake")
    monkeypatch.setattr(P, "extract_pdf_text", lambda p: "CV texte")
    fake_llm.responses = [FACTS_JSON]

    facts = P.load_or_extract_facts(conn, fake_llm, cv)
    assert len(facts) == 2 and facts[0].tech == ["LangChain", "FAISS"]
    assert fake_llm.calls == 1

    again = P.load_or_extract_facts(conn, fake_llm, cv)
    assert [f.id for f in again] == [f.id for f in facts]
    assert fake_llm.calls == 1  # cache : aucun second appel


def test_changed_cv_reextracts(conn, fake_llm, tmp_path, monkeypatch):
    cv = tmp_path / "cv.pdf"
    cv.write_bytes(b"v1")
    monkeypatch.setattr(P, "extract_pdf_text", lambda p: "x")
    fake_llm.responses = [FACTS_JSON, FACTS_JSON]
    P.load_or_extract_facts(conn, fake_llm, cv)
    cv.write_bytes(b"v2")
    P.load_or_extract_facts(conn, fake_llm, cv)
    assert fake_llm.calls == 2


def test_format_and_select(conn, fake_llm, tmp_path, monkeypatch):
    cv = tmp_path / "cv.pdf"; cv.write_bytes(b"z")
    monkeypatch.setattr(P, "extract_pdf_text", lambda p: "x")
    fake_llm.responses = [FACTS_JSON]
    facts = P.load_or_extract_facts(conn, fake_llm, cv)
    txt = P.format_facts(facts)
    assert f"[{facts[0].id}]" in txt and "10 000 articles" in txt
    assert P.facts_by_ids(facts, [facts[1].id]) == [facts[1]]


def test_empty_pdf_text_raises(conn, fake_llm, tmp_path, monkeypatch):
    cv = tmp_path / "cv.pdf"; cv.write_bytes(b"z")
    monkeypatch.setattr(P, "extract_pdf_text", lambda p: "   ")
    with pytest.raises(ValueError):
        P.load_or_extract_facts(conn, fake_llm, cv)
