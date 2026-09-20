from __future__ import annotations

import html as htmllib
import json
import logging
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import requests
from bs4 import BeautifulSoup
from langdetect import DetectorFactory, LangDetectException, detect

from stagefinder.llm import BudgetExceeded, LLMError, LLMFatal

DetectorFactory.seed = 0
log = logging.getLogger(__name__)

SYSTEM = """Tu lis une offre d'emploi et tu en extrais une structure JSON stricte :
{"missions": "résumé fidèle des missions en 2-4 phrases, dans la langue de l'offre",
 "requirements": ["exigences / profil recherché, une par entrée"],
 "tech_stack": ["outils, langages, frameworks et plateformes NOMMÉS explicitement cités"],
 "team_context": "équipe, produit, contexte, 1-2 phrases ou \"\""}

Règle stricte pour "tech_stack" : uniquement des noms propres d'outils, langages, frameworks,
bibliothèques ou plateformes concrets (ex: Python, Docker, LangChain, PostgreSQL, AWS, Kubernetes).
N'y mets JAMAIS un domaine, un champ ou une discipline générique, même s'il est cité dans l'offre
(exclus : "Intelligence Artificielle", "Machine Learning", "IA générative", "Data Science",
"Generative AI", "LLMs" en tant que catégorie, "Deep Learning", "NLP", "Big Data"). Si l'offre ne
cite aucun outil nommé, laisse "tech_stack" vide plutôt que d'y mettre le domaine du poste.

N'invente rien : si une information est absente, laisse une chaîne vide ou une liste vide."""


def fetch_page(url: str, timeout: int = 15) -> str:
    r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=timeout)
    r.raise_for_status()
    return r.text


def html_to_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "nav", "header", "footer", "noscript"]):
        tag.decompose()
    text = soup.get_text("\n")
    text = htmllib.unescape(text)
    lines = [ln.strip() for ln in text.splitlines()]
    return "\n".join(ln for ln in lines if ln)


def detect_language(text: str) -> str:
    try:
        return detect(text) if text.strip() else "und"
    except LangDetectException:
        return "und"


def _fail(conn: sqlite3.Connection, job_id: int, reason: str) -> None:
    conn.execute("UPDATE jobs SET status='failed_enrich', failure_reason=? WHERE id=?",
                 (reason[:500], job_id))
    conn.commit()


def _raise(msg: str):
    """Callable `fetch`-compatible qui relève l'erreur déjà survenue dans le pool de threads,
    pour laisser enrich_job appliquer sa seule règle de repli (page -> description -> échec)."""
    def f(url: str, timeout: int = 15):
        raise RuntimeError(msg)
    return f


DESC_FETCH_THRESHOLD = 500


def enrich_job(conn: sqlite3.Connection, llm, job_id: int, fetch=fetch_page,
               page: str | None = None) -> bool:
    row = conn.execute("SELECT url, description FROM jobs WHERE id=?", (job_id,)).fetchone()
    desc = htmllib.unescape(row["description"] or "")
    if len(desc) >= DESC_FETCH_THRESHOLD:
        text, source = desc, "search_description"
    else:
        try:
            html = page if page is not None else fetch(row["url"], timeout=15)
        except Exception as e:
            if desc.strip():
                text, source = desc, "search_description"
            else:
                _fail(conn, job_id, f"fetch: {e}")
                return False
        else:
            text, source = html_to_text(html), "page"
            if len(text) < 200 and desc.strip():  # page vide / anti-bot : on retombe sur la description
                text, source = desc, "search_description"
    if not text.strip():
        _fail(conn, job_id, "aucun texte exploitable")
        return False
    try:
        data = llm.chat_json(SYSTEM, text[:8000], max_tokens=700)
    except (BudgetExceeded, LLMFatal):
        raise
    except LLMError as e:
        _fail(conn, job_id, f"llm: {e}")
        return False
    if not isinstance(data, dict):
        _fail(conn, job_id, "llm: réponse non conforme (objet JSON attendu)")
        return False
    enriched = {"missions": str(data.get("missions", "")),
                "requirements": [str(x) for x in data.get("requirements", []) or []],
                "tech_stack": [str(x) for x in data.get("tech_stack", []) or []],
                "team_context": str(data.get("team_context", "")),
                "text_source": source}
    lang_text = enriched["missions"] + " " + " ".join(enriched["requirements"])
    language = detect_language(lang_text) if len(lang_text) >= 40 else detect_language(text)
    conn.execute(
        "UPDATE jobs SET status='enriched', full_description=?, enriched=?, language=?, "
        "failure_reason=NULL WHERE id=?",
        (text, json.dumps(enriched, ensure_ascii=False), language, job_id))
    conn.commit()
    return True


def enrich_pending(conn: sqlite3.Connection, llm, fetch=fetch_page, workers: int = 4) -> dict:
    rows = conn.execute(
        "SELECT id, url, description FROM jobs WHERE status='discovered' AND duplicate_of IS NULL "
        "ORDER BY id"
    ).fetchall()
    counters = {"enriched": 0, "failed": 0}
    if not rows:
        return counters

    def _is_long_desc(r) -> bool:
        return len(htmllib.unescape(r["description"] or "")) >= DESC_FETCH_THRESHOLD

    to_fetch = [r for r in rows if not _is_long_desc(r)]

    def get(r):
        try:
            return r["id"], fetch(r["url"], timeout=15), None
        except Exception as e:
            return r["id"], None, str(e)

    # I/O réseau en parallèle (uniquement pour les offres à description courte) ;
    # LLM + écriture SQLite en série sur le thread principal
    with ThreadPoolExecutor(max_workers=workers) as ex:
        pages = list(ex.map(get, to_fetch))
    pages_by_id = {job_id: (page, err) for job_id, page, err in pages}

    for r in rows:
        job_id = r["id"]
        if _is_long_desc(r):
            ok = enrich_job(conn, llm, job_id, fetch=fetch, page=None)
            counters["enriched" if ok else "failed"] += 1
            continue
        page, err = pages_by_id[job_id]
        if err is not None:
            ok = enrich_job(conn, llm, job_id, fetch=_raise(err))
            counters["enriched" if ok else "failed"] += 1
            continue
        ok = enrich_job(conn, llm, job_id, page=page)
        counters["enriched" if ok else "failed"] += 1
    return counters
