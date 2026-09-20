from __future__ import annotations

import json
import sqlite3

from stagefinder.db import now
from stagefinder.llm import BudgetExceeded, LLMError, LLMFatal
from stagefinder.models import Campaign, Fact, campaign_terms
from stagefinder.profile import format_facts

SYSTEM = """Tu es un recruteur. Le candidat cherche : {intent}

Profil du candidat (faits) :
{facts}

Pour chaque offre, évalue de 0 à 100 à quel point elle correspond à ce que cherche le candidat
ET à son profil. Donne 1 à 3 raisons courtes et concrètes (pas de généralités).
Réponds UNIQUEMENT en JSON : [{{"index": 0, "score": 85, "reasons": ["..."]}}, ...]
Inclus TOUS les indices reçus."""


def rule_prefilter(text: str, must_have: list[str], exclude: list[str]) -> bool:
    t = (text or "").lower()
    if any(x.lower() in t for x in exclude):
        return False
    return not must_have or any(m.lower() in t for m in must_have)


def _set(conn, job_id, status, reason=None):
    conn.execute("UPDATE jobs SET status=?, failure_reason=? WHERE id=?", (status, reason, job_id))


def _job_text(row: sqlite3.Row) -> str:
    return f"{row['title']} {row['company']} {row['full_description'] or row['description'] or ''}"


def _as_int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def score_pending(conn: sqlite3.Connection, llm, campaign: Campaign, facts: list[Fact],
                  batch_size: int = 5) -> dict:
    must_have = campaign_terms(conn, campaign.id, "must_have")
    exclude = campaign_terms(conn, campaign.id, "exclude")
    rows = conn.execute(
        "SELECT j.* FROM jobs j JOIN job_campaigns jc ON jc.job_id=j.id "
        "WHERE jc.campaign_id=? AND j.status='enriched' ORDER BY j.id", (campaign.id,)).fetchall()
    counters = {"scored": 0, "skipped": 0, "failed": 0}

    to_llm = []
    for r in rows:
        if rule_prefilter(_job_text(r), must_have, exclude):
            to_llm.append(r)
        else:
            _set(conn, r["id"], "skipped")
            counters["skipped"] += 1
    conn.commit()

    system = SYSTEM.format(intent=campaign.intent, facts=format_facts(facts))
    for i in range(0, len(to_llm), batch_size):
        batch = to_llm[i:i + batch_size]
        user = ""
        for k, r in enumerate(batch):
            e = json.loads(r["enriched"] or "{}")
            user += (f"[{k}] {r['title']} — {r['company']} ({r['location']})\n"
                     f"Missions: {e.get('missions', '')[:600]}\n"
                     f"Exigences: {'; '.join(e.get('requirements', [])[:8])}\n"
                     f"Stack: {', '.join(e.get('tech_stack', [])[:12])}\n\n")
        try:
            answer = llm.chat_json(system, user, max_tokens=600)
        except (BudgetExceeded, LLMFatal):
            raise
        except LLMError as e:
            for r in batch:
                _set(conn, r["id"], "failed_score", f"llm: {e}")
                counters["failed"] += 1
            conn.commit()
            continue
        if not isinstance(answer, list):
            reason = "llm: réponse non conforme (liste attendue)"
            for r in batch:
                _set(conn, r["id"], "failed_score", reason)
                counters["failed"] += 1
            conn.commit()
            continue
        by_index = {}
        for m in answer:
            if isinstance(m, dict) and "score" in m:
                idx = _as_int(m.get("index"))
                if idx is not None and idx not in by_index:  # premier gagne : pas d'écrasement silencieux
                    by_index[idx] = m
        for k, r in enumerate(batch):
            m = by_index.get(k)
            if m is None:
                _set(conn, r["id"], "failed_score", "indice absent de la réponse LLM")
                counters["failed"] += 1
                continue
            score = _as_int(m.get("score"))
            if score is None:
                _set(conn, r["id"], "failed_score", "score non numérique dans la réponse LLM")
                counters["failed"] += 1
                continue
            score = max(0, min(100, score))
            reasons = [str(x) for x in (m.get("reasons") or [])]
            conn.execute(
                "INSERT OR REPLACE INTO scores(job_id,campaign_id,score,reasons,model,scored_at) "
                "VALUES (?,?,?,?,?,?)",
                (r["id"], campaign.id, score, json.dumps(reasons, ensure_ascii=False),
                 getattr(llm, "model", "fake"), now()))
            if score >= campaign.score_threshold:
                _set(conn, r["id"], "scored")
                counters["scored"] += 1
            else:
                _set(conn, r["id"], "skipped")
                counters["skipped"] += 1
        conn.commit()
    return counters
