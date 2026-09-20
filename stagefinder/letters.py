from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

from docx import Document

from stagefinder.db import now
from stagefinder.dedup import normalize_company, normalize_title
from stagefinder.enrich import detect_language
from stagefinder.llm import BudgetExceeded, LLMError, LLMFatal
from stagefinder.models import Campaign, Fact, load_enriched
from stagefinder.profile import facts_by_ids, format_facts

MIN_WORDS, MAX_WORDS = 250, 350

BLACKLIST = {
    "en": ["highly motivated", "detail-oriented", "excellent fit", "passionate about",
           "drive business value", "drive business growth", "team player", "think outside the box",
           "i am confident that", "fast learner"],
    "fr": ["très motivé", "rigoureux et", "force de proposition", "esprit d'équipe",
           "je suis convaincu que", "passionné par", "dynamique et", "polyvalent",
           "je suis enthousiaste à l'idée de", "seront un atout", "mes compétences techniques et mes expériences"],
}
LANG_NAMES = {"fr": "français", "en": "anglais", "de": "allemand", "es": "espagnol", "it": "italien"}

ANGLE_SYSTEM = """Tu prépares une lettre de motivation. Tu ne rédiges PAS : tu choisis l'angle.
À partir de l'offre et des FAITS du candidat (identifiés par [id]), produis un JSON strict :
{"language": "{lang}",
 "company_hooks": ["2-3 éléments précis de l'offre/entreprise à reprendre"],
 "pairs": [{"requirement": "exigence de l'offre", "fact_id": <id>, "why": "en quoi ce fait y répond, avec le détail ou le chiffre concret du fait"}],
 "gaps": ["exigences que les faits ne couvrent pas"],
 "opening_angle": "l'idée d'accroche, une phrase"}

Règles de sélection des faits, dans cet ordre de préférence :
1. Priorité absolue aux faits "project"/"experience"/"achievement" qui portent un chiffre (metric) ou une réalisation nommée — jamais un fait "skill" tant qu'un fait project/experience concret couvre la même exigence.
2. Un fait "skill" (liste d'outils) n'est acceptable que si aucun project/experience du candidat ne touche à cette exigence.
3. "why" doit citer le détail concret ou le chiffre du fait (ex: "pipeline ayant traité 10 000+ articles", pas "le candidat est compétent en Python").
4. Une paire dont le "why" ne fait que reformuler l'exigence sans preuve concrète est inutile : n'en propose pas.

Choisis 2 à 3 paires maximum, les plus fortes. Un fact_id doit exister dans la liste. Ne crée aucun fait."""

WRITE_SYSTEM = """Tu rédiges une lettre de motivation en {lang_name}, {min}-{max} mots, prête à envoyer.
{min} mots est un MINIMUM à respecter, pas une cible haute : une lettre de {min_minus} mots est un échec.
Si le contenu concret semble s'épuiser avant {min} mots, développe davantage le lien entre chaque fait
cité et l'exigence du poste (le contexte du projet, la méthode utilisée, le résultat obtenu) plutôt que
d'ajouter une phrase générique.
Règles absolues :
- Tu n'utilises QUE les faits fournis ci-dessous, et tu dois citer leur contenu concret (le projet, l'outil précis, le chiffre) — jamais une paraphrase de l'exigence du poste.
- N'évoque JAMAIS un outil, une techno ou une méthode qui vient de l'offre (stack, missions, contexte) si elle n'apparaît pas explicitement dans les FAITS ci-dessous, même pour dire que tu es "à l'aise avec" ou "familier de" — ne mentionne que ce que tes faits prouvent réellement.
- Chaque paragraphe central relie UNE exigence de l'offre à UN fait : nomme le projet/l'expérience, cite le chiffre ou l'outil exact, puis dis en une phrase pourquoi cela répond au besoin. N'écris jamais une phrase qui pourrait s'appliquer à n'importe quel candidat (interdit : "je maîtrise X, ce qui est essentiel pour Y", "mon expérience correspond parfaitement à l'exigence de Z").
- N'évoque jamais un manque, une lacune ou une expérience insuffisante : si un gap existe, ignore-le silencieusement plutôt que de t'excuser.
- Le dernier paragraphe avant la formule de politesse doit lui aussi s'appuyer sur un fait concret ou l'accroche entreprise — jamais une clôture générique du type "je suis convaincu que mes compétences seront un atout" ou "je suis enthousiaste à l'idée de rejoindre l'équipe".
- Cite le nom exact de l'entreprise et l'intitulé exact du poste.
- Interdit, sous toute forme même reformulée : {blacklist}.
- Pas de titre, pas d'objet, pas de coordonnées, pas de préambule ("Voici la lettre", markdown, ```) : commence directement par la formule d'appel, termine par la formule de politesse SEULE, sans nom ni placeholder de type "[Votre Nom]", "[Nom Prénom]" ou toute mention entre crochets.
- Ton : direct, factuel, sans flatterie ni superlatif.
{instruction}"""


def _lang_for(campaign: Campaign, job: sqlite3.Row, override: str | None) -> tuple[str, bool]:
    """Retourne (langue, fallback) : fallback est vrai quand `auto` n'a pas pu suivre l'offre."""
    if override:
        return override, False
    if campaign.letter_language != "auto":
        return campaign.letter_language, False
    lang = job["language"] or "und"
    if lang in LANG_NAMES:
        return lang, False
    return "en", True


def _job_block(job: sqlite3.Row) -> str:
    e = load_enriched(job)
    return (f"Poste : {job['title']}\nEntreprise : {job['company']}\nLieu : {job['location']}\n"
            f"Missions : {e['missions']}\nExigences : {'; '.join(e['requirements'])}\n"
            f"Stack : {', '.join(e['tech_stack'])}\nContexte : {e['team_context']}\n"
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
    pairs = angle.get("pairs") or []
    if not isinstance(pairs, list):
        raise LLMError("angle: pairs non conforme")
    valid_ids = {f.id for f in facts}
    angle["pairs"] = [p for p in pairs if isinstance(p, dict) and p.get("fact_id") in valid_ids][:3]
    angle.setdefault("language", language)
    return angle


_FENCE_BLOCK = re.compile(r"```(?:[a-zA-Z]*\n)?(.*?)```", re.S)
_SALUTATION = re.compile(r"^(Madame|Monsieur|Dear|Bonjour|Hello|Sehr|Estimad)", re.I)


def _strip_llm_chrome(text: str) -> str:
    m = _FENCE_BLOCK.search(text)
    if m:
        text = m.group(1)
    text = text.replace("**", "").replace("__", "")
    lines = text.split("\n")
    for i, ln in enumerate(lines):
        s = ln.strip()
        if not s:
            continue
        if s.endswith((",", ":")) or _SALUTATION.match(s):
            return "\n".join(lines[i:]).strip()
    return text.strip()


def write_letter(llm, job, angle: dict, cited: list[Fact], language: str,
                 instruction: str = "", feedback: list[str] | None = None) -> str:
    system = WRITE_SYSTEM.format(
        lang_name=LANG_NAMES.get(language, language), min=MIN_WORDS, max=MAX_WORDS,
        min_minus=MIN_WORDS - 30,
        blacklist=", ".join(BLACKLIST.get(language, BLACKLIST["en"])),
        instruction=f"Consigne supplémentaire : {instruction}" if instruction else "")
    user = (f"{_job_block(job)}\n\nANGLE :\n{json.dumps(angle, ensure_ascii=False)}\n\n"
            f"FAITS AUTORISÉS :\n{format_facts(cited)}")
    if instruction:
        user += f"\n\nConsigne de l'utilisateur : {instruction}"
    if feedback:
        user += "\n\nTa version précédente a été rejetée pour : " + " | ".join(feedback) + \
                "\nCorrige ces points précisément."
    raw = llm.chat(system, user, temperature=0.4, max_tokens=900).strip()
    return _strip_llm_chrome(raw)


_NUM = re.compile(r"\b\d[\d  .,]*\d\b|\b\d\b")


def validate_letter(body: str, job, cited: list[Fact], job_tech: list[str],
                    language: str, all_facts: list[Fact] | None = None) -> list[str]:
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
    if re.search(r"\[[^\]]{2,40}\]", body):
        issues.append("placeholder entre crochets non résolu (ex: [Votre Nom])")
    company_norm = normalize_company(job["company"])
    if not company_norm:
        issues.append("nom de l'entreprise inconnu")
    elif company_norm not in normalize_company(body):
        issues.append(f"nom de l'entreprise absent : {job['company']}")
    title_toks = [t for t in normalize_title(job["title"]).split() if len(t) > 2]
    if title_toks and sum(t in low for t in title_toks) < max(1, len(title_toks) // 2):
        issues.append(f"intitulé du poste absent : {job['title']}")
    cited_tech = {t.lower() for f in cited for t in f.tech}
    for tech in job_tech:
        if re.search(rf"(?<!\w){re.escape(tech.lower())}(?!\w)", low) and tech.lower() not in cited_tech:
            issues.append(f"techno revendiquée hors faits cités : {tech}")
    if all_facts is not None:
        all_tech = {t for f in all_facts for t in f.tech}
        for tech in all_tech:
            if tech.lower() in cited_tech:
                continue
            if re.search(rf"(?<!\w){re.escape(tech.lower())}(?!\w)", low):
                issues.append(f"techno hors faits cités : {tech}")
    allowed = " ".join(f"{f.detail} {f.metric} {f.period} {f.label}" for f in cited).lower()
    allowed += " " + (job["full_description"] or "").lower()
    allowed_nums = {re.sub(r"[  .,]", "", n) for n in _NUM.findall(allowed)}
    for n in _NUM.findall(body):
        clean = re.sub(r"[  .,]", "", n)
        if len(clean) >= 2 and not re.fullmatch(r"20\d\d", clean) and clean not in allowed_nums:
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
    lang, lang_fallback = _lang_for(campaign, job, language)
    job_tech = load_enriched(job)["tech_stack"]
    extra_warnings = []
    if lang_fallback:
        extra_warnings.append("langue de l'offre inconnue : anglais utilisé par défaut")
    try:
        angle = build_angle(llm, job, facts, reasons, lang, instruction)
        pair_ids = [p["fact_id"] for p in angle["pairs"]]
        cited = facts_by_ids(facts, pair_ids)
        if not cited:
            cited = facts[:3]
            extra_warnings.append("angle sans paire valide : 3 premiers faits utilisés")
        body = write_letter(llm, job, angle, cited, lang, instruction)
        issues = validate_letter(body, job, cited, job_tech, lang, facts)
        if issues:
            body = write_letter(llm, job, angle, cited, lang, instruction, feedback=issues)
            issues = validate_letter(body, job, cited, job_tech, lang, facts)
        issues = extra_warnings + issues
    except (BudgetExceeded, LLMFatal):
        raise
    except LLMError as e:
        conn.execute("UPDATE jobs SET status='failed_letter', failure_reason=? WHERE id=?",
                     (f"llm: {e}", job_id))
        conn.commit()
        raise
    version = 1 + (conn.execute(
        "SELECT COALESCE(MAX(version),0) FROM letters WHERE job_id=? AND campaign_id=?",
        (job_id, campaign.id)).fetchone()[0])
    path = Path(letters_dir) / f"{job_id}_{_safe(job['company'])}_{_safe(job['title'])}_v{version}.docx"
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
        except (BudgetExceeded, LLMFatal):
            raise
        except LLMError:
            counters["failed"] += 1
    return counters
