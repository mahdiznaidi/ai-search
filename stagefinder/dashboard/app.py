from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import streamlit as st

from stagefinder import db, letters
from stagefinder.config import load_settings
from stagefinder.dashboard import data as D
from stagefinder.keywords import suggest_terms
from stagefinder.llm import LLMClient, LLMError
from stagefinder.models import add_terms, create_campaign, load_campaign
from stagefinder.profile import cv_hash, forget_facts, load_or_extract_facts

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

            n_keywords = sum(1 for t in terms if t["kind"] == "keyword" and t["enabled"])
            n_locations = sum(1 for t in terms if t["kind"] == "location" and t["enabled"]) or 1
            st.caption(f"≈ {n_keywords * n_locations} combinaisons mot-clé × ville à explorer — "
                      "comptez de quelques minutes à plusieurs dizaines de minutes selon le "
                      "nombre d'offres trouvées.")

            latest = D.latest_run(conn, c["id"])
            run_in_progress = latest is not None and latest["status"] == "running"
            if run_in_progress:
                st.warning(f"Un run est déjà en cours pour cette campagne (run {latest['id']}). "
                          "Attendez sa fin avant d'en relancer un — voir l'écran « Run ».")
            if st.button("Lancer", key=f"run_{c['id']}", disabled=run_in_progress):
                subprocess.Popen(
                    [sys.executable, "-m", "stagefinder.cli", "run",
                     "--campaign", str(c["id"]), "--limit", str(int(limit))],
                    cwd=_project_root())
                st.info(f"Run lancé en arrière-plan pour la campagne {c['id']}. "
                       "Voir l'écran « Run » pour suivre l'avancement.")

            st.divider()
            confirm = st.checkbox("Confirmer la suppression de cette campagne",
                                  key=f"confirm_delete_{c['id']}")
            if st.button("Supprimer la campagne", key=f"delete_{c['id']}", disabled=not confirm):
                D.delete_campaign(conn, c["id"])
                st.success(f"Campagne « {c['name']} » supprimée.")
                st.rerun()


_FRIENDLY_ERRORS = [
    ("HTTP 401", "clé API invalide ou expirée — vérifiez OPENROUTER_API_KEY dans .env"),
    ("HTTP 403", "accès refusé par le fournisseur IA — vérifiez votre clé et vos droits"),
    ("HTTP 402", "crédit épuisé sur votre compte OpenRouter"),
    ("budget:", "plafond d'appels IA atteint pour ce run (voir MAX_LLM_CALLS_PER_RUN)"),
    ("CV_PATH", "CV introuvable — vérifiez le chemin CV_PATH dans .env"),
    ("déjà en cours", "un autre run était déjà en cours pour cette campagne"),
]

_STAGE_LABELS = {"init": "préparation", "discover": "recherche des offres",
                 "enrich": "lecture détaillée des offres", "score": "notation des offres",
                 "generate": "rédaction des lettres"}


def _friendly_error(error: str) -> str:
    for needle, message in _FRIENDLY_ERRORS:
        if needle in error:
            return message
    return error


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
    if latest["status"] == "running":
        st.markdown('<meta http-equiv="refresh" content="5">', unsafe_allow_html=True)
        st.caption("🔄 Cette page se rafraîchit automatiquement toutes les 5 secondes.")

    stage_label = _STAGE_LABELS.get(latest["stage"], latest["stage"] or "—")
    st.markdown(f"**Dernier run [{latest['id']}] : {latest['status']}** — étape : "
               f"{stage_label} — appels IA : {latest['llm_calls']}")
    if latest["counters"]:
        st.json(latest["counters"])
    if latest["error"]:
        st.error(_friendly_error(latest["error"]))

    with st.expander("Historique des runs"):
        for r in runs:
            st.text(f"[{r['id']}] {r['status']} — {r['stage'] or '—'} — {r['started_at']}")


JOBS_FILTERS = {
    "Toutes": None,
    "Prêtes (lettre générée)": ["letter_ready"],
    "Retenues (score suffisant)": ["scored"],
    "Écartées": ["skipped"],
    "En erreur": ["failed_enrich", "failed_score", "failed_letter"],
}


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
    filter_label = st.selectbox("Filtre", list(JOBS_FILTERS.keys()), key="jobs_status_filter")

    jobs = D.list_jobs(conn, campaign_id=cid, status=JOBS_FILTERS[filter_label])
    if not jobs:
        st.info("Aucune offre pour ce filtre.")
        return

    job_titles = {o["id"]: o["title"] for o in jobs}
    for j in jobs:
        cols = st.columns([3, 2, 1, 1, 2])
        cols[0].markdown(f"**{j['title']}** — {j['company']} ({j['location']})")
        cols[1].text(f"statut : {j['status']}")
        cols[2].text(f"score : {j['score'] if j['score'] is not None else '—'}")
        cols[3].markdown(f"[lien]({j['url']})" if j["url"] else "")
        if j["reasons"]:
            cols[0].caption(" · ".join(j["reasons"]))
        other_ids = [o["id"] for o in jobs if o["id"] != j["id"]]
        if other_ids:
            target = cols[4].selectbox(
                "doublon de", other_ids, key=f"dup_target_{j['id']}",
                format_func=lambda i: f"{job_titles[i]} (#{i})", label_visibility="collapsed")
            if cols[4].button("Marquer doublon", key=f"mark_dup_{j['id']}"):
                D.mark_duplicate(conn, j["id"], target)
                st.rerun()


def _page_letters() -> None:
    st.header("Lettres")
    conn = _conn()
    campaigns = D.list_campaigns(conn)
    if not campaigns:
        st.info("Aucune campagne. Créez-en une dans l'écran Campagnes.")
        return

    options = {c["id"]: c["name"] for c in campaigns}
    cid = st.selectbox("Campagne", list(options.keys()), format_func=lambda i: options[i],
                       key="letters_campaign_select")

    letters_rows = D.list_letters(conn, campaign_id=cid)
    if not letters_rows:
        st.info("Aucune lettre pour cette campagne.")
        return

    for row in letters_rows:
        n_warnings = len(row["warnings"])
        badge = "🟢" if n_warnings == 0 else ("🟡" if n_warnings <= 2 else "🔴")
        with st.expander(f"{badge} [v{row['version']}] {row['company']} — {row['title']}"):
            body = conn.execute("SELECT body FROM letters WHERE id=?", (row["id"],)).fetchone()[0]
            edited = st.text_area("Contenu", value=body, height=250, key=f"body_{row['id']}",
                                  label_visibility="collapsed")
            if edited != body and st.button("Enregistrer les modifications", key=f"save_{row['id']}"):
                conn.execute("UPDATE letters SET body=? WHERE id=?", (edited, row["id"]))
                conn.commit()
                path = Path(row["file_path"])
                letters.render_docx(edited, path)
                st.success("Modifications enregistrées et .docx régénéré.")
                st.rerun()
            for w in row["warnings"]:
                st.warning(w)
            path = Path(row["file_path"])
            if path.exists():
                st.download_button("Télécharger le .docx", data=path.read_bytes(),
                                   file_name=path.name, key=f"dl_{row['id']}")

            instruction = st.text_input("Consigne de régénération (optionnel)",
                                        key=f"instr_{row['job_id']}")
            language = st.selectbox("Langue", ["(garder)", "fr", "en", "de", "es", "it"],
                                    key=f"lang_{row['job_id']}")
            if st.button("Régénérer", key=f"regen_{row['job_id']}"):
                llm = _llm()
                campaign = load_campaign(conn, cid)
                facts = _facts_for_dashboard(conn, llm)
                try:
                    letters.generate_for_job(
                        conn, llm, row["job_id"], campaign, facts, _settings().letters_dir,
                        instruction=instruction,
                        language=None if language == "(garder)" else language)
                    st.success("Nouvelle version générée.")
                    st.rerun()
                except LLMError as e:
                    st.error(f"Échec de la régénération : {e}")


def _page_tracking() -> None:
    st.header("Suivi des candidatures")
    conn = _conn()

    jobs = conn.execute(
        "SELECT id, title, company FROM jobs WHERE status IN "
        "('letter_ready','applied','answered','rejected') ORDER BY id DESC").fetchall()
    if not jobs:
        st.info("Aucune offre avec lettre prête pour l'instant.")
        return

    options = {j["id"]: f"{j['title']} — {j['company']}" for j in jobs}
    jid = st.selectbox("Offre", list(options.keys()), format_func=lambda i: options[i],
                       key="tracking_job_select")

    applied_at = st.date_input("Date de candidature", key="tracking_applied_at")
    channel = st.selectbox("Canal", ["email", "site carrière", "LinkedIn", "autre"],
                           key="tracking_channel")
    outcome = st.selectbox("Statut", ["pending", "interview", "rejected", "offer"],
                           key="tracking_outcome")
    notes = st.text_area("Notes", key="tracking_notes")

    if st.button("Enregistrer", key="save_tracking"):
        D.upsert_application(conn, jid, None, str(applied_at), channel, outcome, notes)
        st.success("Candidature enregistrée.")
        st.rerun()

    st.divider()
    st.subheader("Historique")
    for a in D.list_applications(conn):
        st.markdown(f"**{a['title']}** — {a['company']} — statut : {a['outcome']} "
                   f"({a['applied_at']}, {a['channel']})")
        if a["notes"]:
            st.caption(a["notes"])


def _page_profile() -> None:
    st.header("Profil (CV)")
    conn = _conn()
    settings = _settings()
    if not settings.cv_path or not settings.cv_path.exists():
        st.info("Aucun CV configuré — renseignez CV_PATH dans .env.")
        return

    if st.button("Ré-analyser mon CV", key="reanalyze_cv"):
        forget_facts(conn, cv_hash(settings.cv_path))
        try:
            load_or_extract_facts(conn, _llm(), settings.cv_path)
            st.success("CV ré-analysé.")
        except (ValueError, LLMError) as e:
            st.error(f"Échec de l'analyse : {e}")
        st.rerun()

    facts = _facts_for_dashboard(conn, _llm())
    if not facts:
        st.info("Aucun fait extrait pour l'instant — cliquez sur « Ré-analyser mon CV ».")
        return

    st.caption(f"{len(facts)} faits utilisés pour juger la pertinence des offres et rédiger "
              "les lettres. Si l'un d'eux est faux ou mal compris, ré-analysez le CV.")
    for f in facts:
        period = f" ({f.period})" if f.period else ""
        st.markdown(f"**{f.label}**{period} — *{f.kind}*")
        if f.detail:
            st.caption(f.detail)
        if f.tech:
            st.caption("Outils cités : " + ", ".join(f.tech))
        if f.metric:
            st.caption(f"Résultat : {f.metric}")
        st.divider()


PAGES = {"Campagnes": _page_campaigns, "Run": _page_run, "Offres": _page_jobs,
         "Lettres": _page_letters, "Suivi": _page_tracking, "Profil": _page_profile}


def main() -> None:
    page = st.sidebar.radio("Écran", list(PAGES.keys()))
    PAGES[page]()


main()
