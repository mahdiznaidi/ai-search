from __future__ import annotations

import argparse
import json
import logging
import os
import sys

from dotenv import dotenv_values

from stagefinder import db, letters, pipeline
from stagefinder.config import load_settings
from stagefinder.llm import LLMClient
from stagefinder.models import add_terms, create_campaign, load_campaign
from stagefinder.profile import load_or_extract_facts
from stagefinder.sources.jobspy_src import JobSpySource
from stagefinder.sources.jobteaser import JobTeaserSource


def _env() -> dict:
    return {**dotenv_values(".env"), **os.environ}


def _open():
    settings = load_settings(_env())
    conn = db.connect(settings.db_path)
    db.migrate(conn)
    return settings, conn


def _llm(settings):
    return LLMClient(settings.openrouter_api_key, settings.llm_model,
                     max_calls=settings.max_llm_calls_per_run)


def cmd_campaign_add(a) -> int:
    settings, conn = _open()
    cid = create_campaign(conn, a.name, a.intent, a.country, a.job_type, a.remote,
                          a.hours_old, a.letter_language, a.threshold)
    print(f"campagne {cid} créée")
    return 0


def cmd_campaign_term(a) -> int:
    settings, conn = _open()
    add_terms(conn, a.campaign_id, a.kind, a.terms)
    print(f"{len(a.terms)} terme(s) {a.kind} ajouté(s) à la campagne {a.campaign_id}")
    return 0


def cmd_campaign_list(a) -> int:
    settings, conn = _open()
    for r in conn.execute("SELECT * FROM campaigns ORDER BY id"):
        print(f"[{r['id']}] {r['name']} — {r['country']} — {r['job_type'] or 'tous'} — "
              f"lettres: {r['letter_language']} — seuil {r['score_threshold']} — "
              f"dernier run: {r['last_run_at'] or 'jamais'}")
    return 0


def cmd_run(a) -> int:
    settings, conn = _open()
    sources = [JobSpySource()]
    if not a.no_jobteaser:
        sources.append(JobTeaserSource())
    run_id = pipeline.run_campaign(conn, settings, a.campaign, sources, _llm(settings),
                                   run_id=a.resume, limit=a.limit)
    r = conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
    print(f"run {run_id}: {r['status']} (étape {r['stage']})")
    if r["counters"]:
        print(json.dumps(json.loads(r["counters"]), ensure_ascii=False))
    if r["error"]:
        print(f"erreur: {r['error']}", file=sys.stderr)
    return 0 if r["status"] == "done" else 1


def cmd_letters_regenerate(a) -> int:
    settings, conn = _open()
    if not settings.cv_path or not settings.cv_path.exists():
        print("CV_PATH absent ou introuvable : impossible de régénérer sans profil", file=sys.stderr)
        return 1
    llm = _llm(settings)
    campaign = load_campaign(conn, a.campaign)
    facts = load_or_extract_facts(conn, llm, settings.cv_path)
    lid = letters.generate_for_job(conn, llm, a.job_id, campaign, facts, settings.letters_dir,
                                   instruction=a.instruction or "", language=a.language)
    r = conn.execute("SELECT version, file_path, warnings FROM letters WHERE id=?", (lid,)).fetchone()
    print(f"lettre v{r['version']} → {r['file_path']}")
    if json.loads(r["warnings"]):
        print("avertissements:", "; ".join(json.loads(r["warnings"])))
    return 0


_RETRY_STAGES = {
    "enrich": ("failed_enrich", "discovered"),
    "score": ("failed_score", "enriched"),
    "letter": ("failed_letter", "scored"),
}


def cmd_jobs_retry_failed(a) -> int:
    settings, conn = _open()
    from_status, to_status = _RETRY_STAGES[a.stage]
    if a.campaign is not None:
        cur = conn.execute(
            "UPDATE jobs SET status=?, failure_reason=NULL WHERE status=? AND id IN "
            "(SELECT job_id FROM job_campaigns WHERE campaign_id=?)",
            (to_status, from_status, a.campaign))
    else:
        cur = conn.execute(
            "UPDATE jobs SET status=?, failure_reason=NULL WHERE status=?",
            (to_status, from_status))
    conn.commit()
    print(f"{cur.rowcount} offre(s) remise(s) en statut {to_status}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="stagefinder")
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("campaign").add_subparsers(dest="sub", required=True)
    add = c.add_parser("add")
    add.add_argument("--name", required=True)
    add.add_argument("--intent", required=True)
    add.add_argument("--country", required=True)
    add.add_argument("--job-type", dest="job_type", default=None)
    add.add_argument("--remote", action="store_true")
    add.add_argument("--hours-old", dest="hours_old", type=int, default=None)
    add.add_argument("--letter-language", dest="letter_language", default="fr",
                     choices=["fr", "en", "auto"])
    add.add_argument("--threshold", type=int, default=70)
    add.set_defaults(fn=cmd_campaign_add)
    term = c.add_parser("term")
    term.add_argument("campaign_id", type=int)
    term.add_argument("--kind", required=True, choices=["keyword", "must_have", "exclude", "location"])
    term.add_argument("terms", nargs="+")
    term.set_defaults(fn=cmd_campaign_term)
    c.add_parser("list").set_defaults(fn=cmd_campaign_list)

    r = sub.add_parser("run")
    r.add_argument("--campaign", type=int, required=True)
    r.add_argument("--resume", type=int, default=None)
    r.add_argument("--limit", type=int, default=20)
    r.add_argument("--no-jobteaser", dest="no_jobteaser", action="store_true")
    r.set_defaults(fn=cmd_run)

    l = sub.add_parser("letters").add_subparsers(dest="sub", required=True)
    rg = l.add_parser("regenerate")
    rg.add_argument("job_id", type=int)
    rg.add_argument("--campaign", type=int, required=True)
    rg.add_argument("--instruction", default="")
    rg.add_argument("--language", default=None, choices=["fr", "en"])
    rg.set_defaults(fn=cmd_letters_regenerate)

    j = sub.add_parser("jobs").add_subparsers(dest="sub", required=True)
    rf = j.add_parser("retry-failed")
    rf.add_argument("--stage", required=True, choices=["enrich", "score", "letter"])
    rf.add_argument("--campaign", type=int, default=None)
    rf.set_defaults(fn=cmd_jobs_retry_failed)
    return p


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    a = build_parser().parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
