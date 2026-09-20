from __future__ import annotations

import html as htmllib
import json
import logging
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import requests
from bs4 import BeautifulSoup
from langdetect import DetectorFactory, LangDetectException, detect

from stagefinder.db import now
from stagefinder.llm import BudgetExceeded, LLMError

DetectorFactory.seed = 0
log = logging.getLogger(__name__)

SYSTEM = """Tu lis une offre d'emploi et tu en extrais une structure JSON stricte :
{"missions": "résumé fidèle des missions en 2-4 phrases, dans la langue de l'offre",
 "requirements": ["exigences / profil recherché, une par entrée"],
 "tech_stack": ["technologies, outils, langages explicitement cités"],
 "team_context": "équipe, produit, contexte, 1-2 phrases ou \"\""}
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


def enrich_job(conn: sqlite3.Connection, llm, job_id: int, fetch=fetch_page,
               page: str | None = None) -> bool:
    row = conn.execute("SELECT url, description FROM jobs WHERE id=?", (job_id,)).fetchone()
    try:
        html = page if page is not None else fetch(row["url"], timeout=15)
    except Exception as e:
        _fail(conn, job_id, f"fetch: {e}")
        return False
    text = html_to_text(html)
    if len(text) < 200:  # page vide / anti-bot : on retombe sur la description courte
        text = htmllib.unescape(row["description"] or "")
    if not text.strip():
        _fail(conn, job_id, "aucun texte exploitable")
        return False
    try:
        data = llm.chat_json(SYSTEM, text[:8000], max_tokens=700)
    except BudgetExceeded:
        raise
    except LLMError as e:
        _fail(conn, job_id, f"llm: {e}")
        return False
    enriched = {"missions": str(data.get("missions", "")),
                "requirements": [str(x) for x in data.get("requirements", []) or []],
                "tech_stack": [str(x) for x in data.get("tech_stack", []) or []],
                "team_context": str(data.get("team_context", ""))}
    conn.execute(
        "UPDATE jobs SET status='enriched', full_description=?, enriched=?, language=?, "
        "failure_reason=NULL WHERE id=?",
        (text, json.dumps(enriched, ensure_ascii=False), detect_language(text), job_id))
    conn.commit()
    return True


def enrich_pending(conn: sqlite3.Connection, llm, fetch=fetch_page, workers: int = 4) -> dict:
    rows = conn.execute(
        "SELECT id, url FROM jobs WHERE status='discovered' AND duplicate_of IS NULL ORDER BY id"
    ).fetchall()
    counters = {"enriched": 0, "failed": 0}
    if not rows:
        return counters

    def get(r):
        try:
            return r["id"], fetch(r["url"], timeout=15), None
        except Exception as e:
            return r["id"], None, str(e)

    # I/O réseau en parallèle ; LLM + écriture SQLite en série sur le thread principal
    with ThreadPoolExecutor(max_workers=workers) as ex:
        pages = list(ex.map(get, rows))
    for job_id, page, err in pages:
        if err is not None:
            _fail(conn, job_id, f"fetch: {err}")
            counters["failed"] += 1
            continue
        ok = enrich_job(conn, llm, job_id, page=page)
        counters["enriched" if ok else "failed"] += 1
    return counters
