from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

from docx import Document

from stagefinder.db import now
from stagefinder.dedup import normalize_company, normalize_title
from stagefinder.enrich import detect_language
from stagefinder.llm import BudgetExceeded, LLMError
from stagefinder.models import Campaign, Fact
from stagefinder.profile import facts_by_ids, format_facts

MIN_WORDS, MAX_WORDS = 250, 350

BLACKLIST = {
    "en": ["highly motivated", "detail-oriented", "excellent fit", "passionate about",
           "drive business value", "drive business growth", "team player", "think outside the box",
           "i am confident that", "fast learner"],
    "fr": ["très motivé", "rigoureux et", "force de proposition", "esprit d'équipe",
           "je suis convaincu que", "passionné par", "dynamique et", "polyvalent"],
}
LANG_NAMES = {"fr": "français", "en": "anglais", "de": "allemand", "es": "espagnol", "it": "italien"}

ANGLE_SYSTEM = """Tu prépares une lettre de motivation. Tu ne rédiges PAS : tu choisis l'angle.
À partir de l'offre et des FAITS du candidat (identifiés par [id]), produis un JSON strict :
{"language": "{lang}",
 "company_hooks": ["2-3 éléments précis de l'offre/entreprise à reprendre"],
 "pairs": [{"requirement": "exigence de l'offre", "fact_id": <id>, "why": "en quoi ce fait y répond, concrètement"}],
 "gaps": ["exigences que les faits ne couvrent pas"],
 "opening_angle": "l'idée d'accroche, une phrase"}
Choisis 2 à 3 paires maximum. Un fact_id doit exister dans la liste. Ne crée aucun fait."""

WRITE_SYSTEM = """Tu rédiges une lettre de motivation en {lang_name}, {min}-{max} mots, prête à envoyer.
Règles absolues :
- Tu n'utilises QUE les faits fournis ci-dessous. Aucune expérience, techno, chiffre ou diplôme hors de cette liste.
- Chaque paragraphe central relie UNE exigence de l'offre à UN fait, en expliquant le lien (pas d'affirmation sans preuve).
- Cite le nom exact de l'entreprise et l'intitulé exact du poste.
- Interdit : {blacklist}.
- Pas de titre, pas d'objet, pas de coordonnées : commence par la formule d'appel, termine par la formule de politesse.
- Ton : direct, précis, sans flatterie.
{instruction}"""


def _lang_for(campaign: Campaign, job: sqlite3.Row, override: str | None) -> str:
    if override:
        return override
    if campaign.letter_language != "auto":
        return campaign.letter_language
    lang = job["language"] or "und"
    return lang if lang in LANG_NAMES else "en"


def _job_block(job: sqlite3.Row) -> str:
    e = json.loads(job["enriched"] or "{}")
    return (f"Poste : {job['title']}\nEntreprise : {job['company']}\nLieu : {job['location']}\n"
            f"Missions : {e.get('missions', '')}\nExigences : {'; '.join(e.get('requirements', []))}\n"
            f"Stack : {', '.join(e.get('tech_stack', []))}\nContexte : {e.get('team_context', '')}\n"
            f"Extrait : {(job['full_description'] or job['description'] or '')[:1500]}")


def build_angle(llm, job, facts: list[Fact], reasons: list[str], language: str,
                instruction: str = "") -> dict:
    user = (f"{_job_block(job)}\n\nRaisons du matching : {'; '.join(reasons)}\n\n"
            f"FAITS :\n{format_facts(facts)}")
    if instruction:
        user += f"\n\nConsigne de l'utilisateur : {instruction}"
    angle = llm.chat_json(ANGLE_SYSTEM.replace("{lang}", language), user, max_tokens=600)
    if not isinstance(angle, dict):
        raise LLMError("angle: réponse non conforme (objet JSON attendu)")
    valid_ids = {f.id for f in facts}
    angle["pairs"] = [p for p in angle.get("pairs", []) if p.get("fact_id") in valid_ids][:3]
    angle.setdefault("language", language)
    return angle


def write_letter(llm, job, angle: dict, cited: list[Fact], language: str,
                 instruction: str = "", feedback: list[str] | None = None) -> str:
    system = WRITE_SYSTEM.format(
        lang_name=LANG_NAMES.get(language, language), min=MIN_WORDS, max=MAX_WORDS,
        blacklist=", ".join(BLACKLIST.get(language, BLACKLIST["en"])),
        instruction=f"Consigne supplémentaire : {instruction}" if instruction else "")
    user = (f"{_job_block(job)}\n\nANGLE :\n{json.dumps(angle, ensure_ascii=False)}\n\n"
            f"FAITS AUTORISÉS :\n{format_facts(cited)}")
    if instruction:
        user += f"\n\nConsigne de l'utilisateur : {instruction}"
    if feedback:
        user += "\n\nTa version précédente a été rejetée pour : " + " | ".join(feedback) + \
                "\nCorrige ces points précisément."
    return llm.chat(system, user, temperature=0.4, max_tokens=900).strip()


_NUM = re.compile(r"\b\d[\d  .,]*\d\b|\b\d\b")


def validate_letter(body: str, job, cited: list[Fact], job_tech: list[str],
                    language: str) -> list[str]:
    issues = []
    low = body.lower()
    words = len(body.split())
    if not (MIN_WORDS <= words <= MAX_WORDS):
        issues.append(f"longueur {words} mots (attendu {MIN_WORDS}-{MAX_WORDS})")
    if detect_language(body) != language:
        issues.append(f"langue détectée ≠ {language} (langue attendue)")
    for phrase in BLACKLIST.get(language, []):
        if phrase in low:
            issues.append(f"formule interdite : « {phrase} »")
    if normalize_company(job["company"]) not in normalize_company(body):
        issues.append(f"nom de l'entreprise absent : {job['company']}")
    title_toks = [t for t in normalize_title(job["title"]).split() if len(t) > 2]
    if title_toks and sum(t in low for t in title_toks) < max(1, len(title_toks) // 2):
        issues.append(f"intitulé du poste absent : {job['title']}")
    cited_tech = {t.lower() for f in cited for t in f.tech}
    for tech in job_tech:
        if re.search(rf"\b{re.escape(tech.lower())}\b", low) and tech.lower() not in cited_tech:
            issues.append(f"techno revendiquée hors faits cités : {tech}")
    allowed = " ".join(f"{f.detail} {f.metric} {f.period} {f.label}" for f in cited).lower()
    allowed += " " + (job["full_description"] or "").lower()
    for n in _NUM.findall(body):
        clean = re.sub(r"[  .,]", "", n)
        if len(clean) >= 2 and not re.fullmatch(r"20\d\d", clean) and \
                clean not in re.sub(r"[  .,]", "", allowed):
            issues.append(f"chiffre non sourcé : {n.strip()}")
    return issues


def render_docx(body: str, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = Document()
    for para in re.split(r"\n\s*\n", body.strip()):
        doc.add_paragraph(para.strip())
    doc.save(path)


def _safe(s: str) -> str:
    return re.sub(r"[^\w]+", "_", s or "")[:40].strip("_") or "x"


def generate_for_job(conn: sqlite3.Connection, llm, job_id: int, campaign: Campaign,
                     facts: list[Fact], letters_dir: Path, instruction: str = "",
                     language: str | None = None) -> int:
    job = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    sc = conn.execute("SELECT reasons FROM scores WHERE job_id=? AND campaign_id=?",
                      (job_id, campaign.id)).fetchone()
    reasons = json.loads(sc["reasons"]) if sc and sc["reasons"] else []
    lang = _lang_for(campaign, job, language)
    job_tech = json.loads(job["enriched"] or "{}").get("tech_stack", [])
    try:
        angle = build_angle(llm, job, facts, reasons, lang, instruction)
        cited = facts_by_ids(facts, [p["fact_id"] for p in angle["pairs"]]) or facts[:3]
        body = write_letter(llm, job, angle, cited, lang, instruction)
        issues = validate_letter(body, job, cited, job_tech, lang)
        if issues:
            body = write_letter(llm, job, angle, cited, lang, instruction, feedback=issues)
            issues = validate_letter(body, job, cited, job_tech, lang)
    except BudgetExceeded:
        raise
    except LLMError as e:
        conn.execute("UPDATE jobs SET status='failed_letter', failure_reason=? WHERE id=?",
                     (f"llm: {e}", job_id))
        conn.commit()
        raise
    version = 1 + (conn.execute(
        "SELECT COALESCE(MAX(version),0) FROM letters WHERE job_id=? AND campaign_id=?",
        (job_id, campaign.id)).fetchone()[0])
    path = Path(letters_dir) / f"{_safe(job['company'])}_{_safe(job['title'])}_v{version}.docx"
    render_docx(body, path)
    cur = conn.execute(
        "INSERT INTO letters(job_id,campaign_id,version,language,angle,body,file_path,warnings,"
        "model,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (job_id, campaign.id, version, lang, json.dumps(angle, ensure_ascii=False), body,
         str(path), json.dumps(issues, ensure_ascii=False), getattr(llm, "model", "fake"), now()))
    conn.execute("UPDATE jobs SET status='letter_ready', failure_reason=NULL WHERE id=?", (job_id,))
    conn.commit()
    return cur.lastrowid


def generate_pending(conn: sqlite3.Connection, llm, campaign: Campaign, facts: list[Fact],
                     letters_dir: Path) -> dict:
    rows = conn.execute(
        "SELECT j.id FROM jobs j JOIN job_campaigns jc ON jc.job_id=j.id "
        "WHERE jc.campaign_id=? AND j.status='scored' ORDER BY j.id", (campaign.id,)).fetchall()
    counters = {"letters": 0, "failed": 0}
    for r in rows:
        try:
            generate_for_job(conn, llm, r["id"], campaign, facts, letters_dir)
            counters["letters"] += 1
        except BudgetExceeded:
            raise
        except LLMError:
            counters["failed"] += 1
    return counters
