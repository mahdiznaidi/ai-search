from __future__ import annotations

import json
import logging
import random
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from stagefinder import enrich, letters, scoring
from stagefinder.config import Settings
from stagefinder.db import now
from stagefinder.dedup import upsert_job
from stagefinder.llm import BudgetExceeded, LLMFatal
from stagefinder.models import Campaign, campaign_terms, load_campaign
from stagefinder.profile import load_or_extract_facts
from stagefinder.sources.base import Source

log = logging.getLogger(__name__)


def discover(conn: sqlite3.Connection, campaign: Campaign, sources: list[Source],
             limit: int = 20, workers: int = 4) -> dict:
    keywords = campaign_terms(conn, campaign.id, "keyword")
    locations = campaign_terms(conn, campaign.id, "location") or [campaign.country]
    active = [s for s in sources if s.supports(campaign.country)]
    tasks = [(s, k, l) for s in active for k in keywords for l in locations]
    counters = {"found": 0, "new": 0, "duplicates": 0}

    def search(t):
        s, k, l = t
        time.sleep(random.uniform(0, 0.5))  # jitter : rester poli avec les plateformes
        return k, l, s.search(k, l, campaign.country, campaign.job_type, campaign.hours_old,
                              campaign.is_remote, limit)

    # recherche en parallèle ; écriture SQLite en série
    with ThreadPoolExecutor(max_workers=workers) as ex:
        results = list(ex.map(search, tasks))
    for k, l, raws in results:
        for raw in raws:
            counters["found"] += 1
            res = upsert_job(conn, raw, campaign.id, k, l)
            if res.created:
                counters["new"] += 1
            if res.created and res.duplicate_of:
                counters["duplicates"] += 1
    return counters


def _update_run(conn, run_id, **fields):
    sets = ", ".join(f"{k}=?" for k in fields)
    conn.execute(f"UPDATE runs SET {sets} WHERE id=?", (*fields.values(), run_id))
    conn.commit()


def run_campaign(conn: sqlite3.Connection, settings: Settings, campaign_id: int,
                 sources: list[Source], llm, fetch=enrich.fetch_page,
                 run_id: int | None = None, limit: int = 20) -> int:
    if run_id is None:
        cur = conn.execute("INSERT INTO runs(campaign_id,status,started_at) VALUES (?,?,?)",
                           (campaign_id, "running", now()))
        run_id = cur.lastrowid
    else:
        _update_run(conn, run_id, status="running", error=None)
    conn.commit()
    counters: dict = {}
    stage = "init"
    try:
        campaign = load_campaign(conn, campaign_id)
        if not settings.cv_path or not Path(settings.cv_path).exists():
            raise ValueError("CV_PATH absent ou introuvable : impossible de scorer sans profil")
        facts = load_or_extract_facts(conn, llm, Path(settings.cv_path))

        stage = "discover"
        _update_run(conn, run_id, stage=stage)
        counters.update(discover(conn, campaign, sources, limit=limit))
        _update_run(conn, run_id, counters=json.dumps(counters), llm_calls=llm.calls)

        stage = "enrich"
        _update_run(conn, run_id, stage=stage)
        c = enrich.enrich_pending(conn, llm, fetch=fetch)
        counters.update(enriched=c["enriched"], enrich_failed=c["failed"])
        _update_run(conn, run_id, counters=json.dumps(counters), llm_calls=llm.calls)

        stage = "score"
        _update_run(conn, run_id, stage=stage)
        c = scoring.score_pending(conn, llm, campaign, facts)
        counters.update(scored=c["scored"], skipped=c["skipped"], score_failed=c["failed"])
        _update_run(conn, run_id, counters=json.dumps(counters), llm_calls=llm.calls)

        stage = "generate"
        _update_run(conn, run_id, stage=stage)
        c = letters.generate_pending(conn, llm, campaign, facts, Path(settings.letters_dir))
        counters.update(letters=c["letters"], letter_failed=c["failed"])

        conn.execute("UPDATE campaigns SET last_run_at=? WHERE id=?", (now(), campaign_id))
        _update_run(conn, run_id, status="done", stage=stage, counters=json.dumps(counters),
                    llm_calls=llm.calls, finished_at=now())
    except BudgetExceeded as e:
        _update_run(conn, run_id, status="failed", stage=stage, error=f"budget: {e}",
                    counters=json.dumps(counters), llm_calls=llm.calls, finished_at=now())
    except LLMFatal as e:
        _update_run(conn, run_id, status="failed", stage=stage, error=f"llm: {e}",
                    counters=json.dumps(counters), llm_calls=llm.calls, finished_at=now())
    except Exception as e:  # erreur globale : on la rend visible, on ne dégrade pas
        log.exception("run %s échoué à l'étape %s", run_id, stage)
        _update_run(conn, run_id, status="failed", stage=stage, error=f"{type(e).__name__}: {e}",
                    counters=json.dumps(counters), llm_calls=llm.calls, finished_at=now())
    return run_id
