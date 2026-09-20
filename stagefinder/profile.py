from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
from pathlib import Path

from pypdf import PdfReader

from stagefinder.models import Fact, row_to_fact

log = logging.getLogger(__name__)

CV_MAX_CHARS = 15000

SYSTEM = """Tu extrais d'un CV des FAITS atomiques et vérifiables, pour rédiger des lettres de motivation.
Chaque fait : {"kind": "experience|project|skill|education|achievement", "label": "titre court",
"detail": "ce qui a été fait, concrètement, 1-2 phrases", "tech": ["outils cités"], "metric": "chiffre ou résultat s'il existe, sinon \"\"", "period": "dates ou \"\""}.
Règles : n'invente rien, ne reformule pas en plus flatteur, garde les chiffres exacts, une entrée par projet/expérience/diplôme, et une entrée "skill" par groupe de compétences.
Réponds UNIQUEMENT en JSON : {"facts": [...]}"""


def cv_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _tech_list(v) -> list[str]:
    if isinstance(v, list):
        return [str(x).strip() for x in v if str(x).strip()]
    if isinstance(v, str) and v.strip():
        return [t.strip() for t in re.split(r"[,;/]", v) if t.strip()]
    return []


def extract_pdf_text(path: Path) -> str:
    with path.open("rb") as f:
        reader = PdfReader(f)
        return "\n".join((p.extract_text() or "") for p in reader.pages).strip()


def _load(conn: sqlite3.Connection, h: str) -> list[Fact]:
    rows = conn.execute("SELECT * FROM facts WHERE cv_hash=? ORDER BY id", (h,)).fetchall()
    return [row_to_fact(r) for r in rows]


def load_or_extract_facts(conn: sqlite3.Connection, llm, cv_path: Path) -> list[Fact]:
    h = cv_hash(cv_path)
    cached = _load(conn, h)
    if cached:
        return cached
    text = extract_pdf_text(cv_path)
    if not text:
        raise ValueError(f"aucun texte extrait de {cv_path} (PDF scanné ?)")
    if len(text) > CV_MAX_CHARS:
        log.warning("CV %s tronqué à %d caractères (longueur %d)", cv_path, CV_MAX_CHARS, len(text))
    data = llm.chat_json(SYSTEM, f"CV :\n\n{text[:CV_MAX_CHARS]}", max_tokens=2000)
    # Handle bare list response (common LLM drift)
    if isinstance(data, list):
        facts = data
    else:
        facts = data.get("facts") if isinstance(data, dict) else None
    # Validate and filter to dict entries only
    if isinstance(facts, list):
        facts = [f for f in facts if isinstance(f, dict)]
    if not facts:
        raise ValueError("le LLM n'a renvoyé aucun fait exploitable")
    conn.executemany(
        "INSERT INTO facts(cv_hash,kind,label,detail,tech,metric,period) VALUES (?,?,?,?,?,?,?)",
        [(h, f.get("kind", "skill"), f.get("label", ""), f.get("detail", ""),
          json.dumps(_tech_list(f.get("tech")), ensure_ascii=False), f.get("metric", "") or "",
          f.get("period", "") or "") for f in facts])
    conn.commit()
    return _load(conn, h)


def facts_by_ids(facts: list[Fact], ids: list[int]) -> list[Fact]:
    wanted = set(ids)
    return [f for f in facts if f.id in wanted]


def format_facts(facts: list[Fact]) -> str:
    lines = []
    for f in facts:
        tech = f", tech: {', '.join(f.tech)}" if f.tech else ""
        metric = f", résultat: {f.metric}" if f.metric else ""
        period = f" ({f.period})" if f.period else ""
        lines.append(f"[{f.id}] {f.kind} — {f.label}{period}: {f.detail}{tech}{metric}")
    return "\n".join(lines)
