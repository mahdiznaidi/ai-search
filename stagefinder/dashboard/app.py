from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import streamlit as st

from stagefinder import db
from stagefinder.config import load_settings
from stagefinder.dashboard import data as D
from stagefinder.keywords import suggest_terms
from stagefinder.llm import LLMClient, LLMError
from stagefinder.models import add_terms, create_campaign, load_campaign
from stagefinder.profile import load_or_extract_facts

st.set_page_config(page_title="StageFinder", layout="wide")


def _project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _settings():
    return load_settings()


def _conn():
    settings = _settings()
    conn = db.connect(settings.db_path)
    db.migrate(conn)
    return conn


def _llm():
    settings = _settings()
    return LLMClient(settings.openrouter_api_key, settings.llm_model,
                     max_calls=settings.max_llm_calls_per_run)


def _facts_for_dashboard(conn, llm):
    settings = _settings()
    if not settings.cv_path or not settings.cv_path.exists():
        return []
    try:
        return load_or_extract_facts(conn, llm, settings.cv_path)
    except ValueError:
        return []


def _page_campaigns() -> None:
    st.header("Campagnes")
    conn = _conn()

    with st.form("new_campaign_form", clear_on_submit=False):
        st.text_input("Nom", key="new_name")
        st.text_area("Intention (ce que vous cherchez, en une phrase)", key="new_intent")
        st.text_input("Pays (code ISO, ex: FR)", key="new_country")
        st.selectbox("Type de poste", ["", "internship", "fulltime", "contract"], key="new_job_type")
        st.selectbox("Langue des lettres", ["fr", "en", "auto"], key="new_letter_language")
        submitted = st.form_submit_button("Créer", key="create_campaign_btn")
        if submitted:
            name = st.session_state.get("new_name", "").strip()
            intent = st.session_state.get("new_intent", "").strip()
            country = st.session_state.get("new_country", "").strip()
            if name and intent and country:
                create_campaign(conn, name, intent, country,
                                job_type=st.session_state.get("new_job_type") or None,
                                letter_language=st.session_state.get("new_letter_language", "fr"))
                st.success(f"Campagne « {name} » créée.")
            else:
                st.error("Nom, intention et pays sont obligatoires.")

    st.divider()
    for c in D.list_campaigns(conn):
        with st.expander(f"[{c['id']}] {c['name']} — {c['country']} — "
                         f"dernier run : {c['last_run_at'] or 'jamais'}"):
            st.markdown(f"**{c['name']}** — intention : {c['intent']}")

            terms = D.list_campaign_terms(conn, c["id"])
            for t in terms:
                label = f"{t['kind']} : {t['term']} ({t['origin']})"
                checked = st.checkbox(label, value=bool(t["enabled"]), key=f"term_{t['id']}")
                if checked != bool(t["enabled"]):
                    conn.execute("UPDATE campaign_terms SET enabled=? WHERE id=?",
                                (int(checked), t["id"]))
                    conn.commit()

            cols = st.columns(4)
            for i, kind in enumerate(["keyword", "must_have", "exclude", "location"]):
                with cols[i]:
                    st.text_input(kind, key=f"manual_term_{c['id']}_{kind}")
                    if st.button(f"Ajouter ({kind})", key=f"add_manual_{c['id']}_{kind}"):
                        value = st.session_state.get(f"manual_term_{c['id']}_{kind}", "").strip()
                        if value:
                            add_terms(conn, c["id"], kind, [value])
                            st.rerun()

            if st.button("Suggérer avec l'IA", key=f"suggest_{c['id']}"):
                campaign = load_campaign(conn, c["id"])
                llm = _llm()
                try:
                    facts = _facts_for_dashboard(conn, llm)
                    suggestions = suggest_terms(llm, campaign, facts)
                    for kind in ("keyword", "exclude", "location"):
                        if suggestions[kind]:
                            add_terms(conn, c["id"], kind, suggestions[kind], origin="ai")
                    st.success("Suggestions ajoutées (décochez celles à écarter).")
                    st.rerun()
                except LLMError as e:
                    st.error(f"Échec de la suggestion : {e}")

            limit = st.number_input("Limite d'offres par recherche", min_value=1, max_value=50,
                                    value=10, key=f"limit_{c['id']}")
            if st.button("Lancer", key=f"run_{c['id']}"):
                subprocess.Popen(
                    [sys.executable, "-m", "stagefinder.cli", "run",
                     "--campaign", str(c["id"]), "--limit", str(int(limit))],
                    cwd=_project_root())
                st.info(f"Run lancé en arrière-plan pour la campagne {c['id']}. "
                       "Voir l'écran « Run » pour suivre l'avancement.")


def _page_run() -> None:
    st.header("Run")
    conn = _conn()
    campaigns = D.list_campaigns(conn)
    if not campaigns:
        st.info("Aucune campagne. Créez-en une dans l'écran Campagnes.")
        return

    options = {c["id"]: c["name"] for c in campaigns}
    cid = st.selectbox("Campagne", list(options.keys()), format_func=lambda i: options[i],
                       key="run_campaign_select")

    if st.button("Rafraîchir", key="refresh_run"):
        st.rerun()

    runs = D.list_runs(conn, cid)
    if not runs:
        st.info("Aucun run pour cette campagne.")
        return

    latest = runs[0]
    st.markdown(f"**Dernier run [{latest['id']}] : {latest['status']}** — étape : "
               f"{latest['stage'] or '—'} — appels LLM : {latest['llm_calls']}")
    if latest["counters"]:
        st.json(latest["counters"])
    if latest["error"]:
        st.error(latest["error"])

    with st.expander("Historique des runs"):
        for r in runs:
            st.text(f"[{r['id']}] {r['status']} — {r['stage'] or '—'} — {r['started_at']}")


def _page_jobs() -> None:
    st.header("Offres")
    conn = _conn()
    campaigns = D.list_campaigns(conn)
    if not campaigns:
        st.info("Aucune campagne. Créez-en une dans l'écran Campagnes.")
        return

    options = {c["id"]: c["name"] for c in campaigns}
    cid = st.selectbox("Campagne", list(options.keys()), format_func=lambda i: options[i],
                       key="jobs_campaign_select")
    status_filter = st.selectbox(
        "Statut", ["(tous)", "discovered", "enriched", "scored", "skipped", "letter_ready",
                  "duplicate", "failed_enrich", "failed_score", "failed_letter"],
        key="jobs_status_filter")
    status = None if status_filter == "(tous)" else status_filter

    jobs = D.list_jobs(conn, campaign_id=cid, status=status)
    if not jobs:
        st.info("Aucune offre pour ces filtres.")
        return

    for j in jobs:
        cols = st.columns([3, 2, 1, 1, 2])
        cols[0].markdown(f"**{j['title']}** — {j['company']} ({j['location']})")
        cols[1].text(f"statut : {j['status']}")
        cols[2].text(f"score : {j['score'] if j['score'] is not None else '—'}")
        cols[3].markdown(f"[lien]({j['url']})" if j["url"] else "")
        other_ids = [o["id"] for o in jobs if o["id"] != j["id"]]
        if other_ids:
            target = cols[4].selectbox("doublon de", other_ids, key=f"dup_target_{j['id']}",
                                       label_visibility="collapsed")
            if cols[4].button("Marquer doublon", key=f"mark_dup_{j['id']}"):
                D.mark_duplicate(conn, j["id"], target)
                st.rerun()


PAGES = {"Campagnes": _page_campaigns, "Run": _page_run, "Offres": _page_jobs}


def main() -> None:
    page = st.sidebar.radio("Écran", list(PAGES.keys()))
    PAGES[page]()


main()
