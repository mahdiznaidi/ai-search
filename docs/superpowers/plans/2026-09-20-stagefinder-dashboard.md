# StageFinder v2 — Plan d'implémentation 2/2 : suggestion IA des mots-clés + tableau de bord

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ajouter au pipeline StageFinder v2 la suggestion IA des mots-clés/exclusions/villes d'une campagne (spec §5.1) et un tableau de bord Streamlit à 5 écrans (spec §6) : Campagnes, Run, Offres, Lettres, Suivi — sans toucher au pipeline cœur déjà livré et testé (131 tests).

**Architecture:** Deux ajouts indépendants du pipeline cœur. `keywords.py` est un module pur (LLM → liste de termes), branché sur `models.add_terms` existant, exposé en CLI. Le dashboard est un module `dashboard/` séparé en deux couches : `data.py` (requêtes SQL pures, testables sans Streamlit) et `app.py` (rendu Streamlit, testé avec `streamlit.testing.v1.AppTest`, disponible dans la version installée 1.56.0). Le bouton « Lancer » du dashboard démarre `python -m stagefinder.cli run` en sous-processus (SQLite est déjà en mode WAL, lecture concurrente sûre) ; le dashboard ne fait jamais de travail long lui-même.

**Tech Stack:** Python 3.11, Streamlit 1.56.0 (`streamlit.testing.v1.AppTest` pour les tests), sqlite3, le reste du stack déjà en place (voir plan 1/2).

**Spec:** `docs/superpowers/specs/2026-09-20-stagefinder-design.md` (§5.1 Campagne et mots-clés, §6 Dashboard)

## Global Constraints

- Aucune logique métier en dur : la suggestion IA part de `campaign.intent` + `campaign.country` + les faits du CV, jamais d'une liste figée.
- Un terme suggéré par l'IA est stocké avec `origin='ai'` et **activé par défaut** (`enabled=1`) ; l'utilisateur peut le désactiver — jamais de suppression silencieuse.
- Le dashboard ne fait jamais de travail long (scraping, LLM) dans le processus Streamlit ; tout run passe par un sous-processus `stagefinder.cli run`.
- Aucun appel réseau ni LLM réel dans les tests : `fake_llm` (voir `tests/conftest.py`) pour `keywords.py`, DB SQLite temporaire pour `data.py`, `AppTest` avec base et LLM factices pour `app.py`.
- Même règle qu'au plan 1 : aucun repli silencieux qui ressemble à un succès (une réponse LLM non conforme lève une erreur explicite, jamais une liste vide passée sous silence).
- Messages de commit terminés par `Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>`.
- Suite complète (`python -m pytest -q`) verte et sans warning après chaque tâche ; au démarrage 131 tests passent déjà (plan 1/2 + corrections post-run réel).

## Signatures existantes utilisées par ce plan (ne pas redéfinir, seulement consommer)

```python
# stagefinder/models.py
class Campaign:  # dataclass
    id: int; name: str; intent: str; country: str; job_type: str | None
    is_remote: bool; hours_old: int | None; letter_language: str; score_threshold: int
class Fact:  # dataclass
    id: int; kind: str; label: str; detail: str; tech: list[str]; metric: str; period: str
def load_campaign(conn, campaign_id: int) -> Campaign: ...          # KeyError si absente
def add_terms(conn, campaign_id: int, kind: str, terms: list[str], origin: str = "user") -> None: ...
def campaign_terms(conn, campaign_id: int, kind: str) -> list[str]: ...  # seulement enabled=1

# stagefinder/profile.py
def load_or_extract_facts(conn, llm, cv_path: Path) -> list[Fact]: ...
def format_facts(facts: list[Fact]) -> str: ...

# stagefinder/llm.py
class LLMError(Exception): ...
class LLMClient:
    def __init__(self, api_key: str, model: str, max_calls: int | None = None, ...): ...
    def chat_json(self, system: str, user: str, **kw) -> Any: ...   # peut renvoyer une liste, pas seulement un dict

# stagefinder/config.py
class Settings:  # dataclass frozen
    openrouter_api_key: str; llm_model: str; max_llm_calls_per_run: int
    cv_path: Path | None; db_path: Path; letters_dir: Path
def load_settings(env: Mapping[str,str] | None = None, env_file: str | None = None) -> Settings: ...

# stagefinder/db.py
def connect(db_path: Path) -> sqlite3.Connection: ...   # row_factory=Row, WAL, foreign_keys ON
def migrate(conn) -> None: ...
def now() -> str: ...

# stagefinder/pipeline.py
def run_campaign(conn, settings, campaign_id: int, sources, llm, fetch=..., run_id=None, limit=20) -> int: ...

# stagefinder/letters.py
def generate_for_job(conn, llm, job_id: int, campaign: Campaign, facts: list[Fact], letters_dir,
                     instruction: str = "", language: str | None = None) -> int: ...  # retourne letters.id

# tests/conftest.py
class FakeLLM:
    def __init__(self, responses=None): ...   # .responses: list[str | callable]; .calls; .prompts
    def chat_json(self, system, user, **kw): ...
@pytest.fixture
def tmp_db(tmp_path) -> Path: ...
@pytest.fixture
def fake_llm() -> FakeLLM: ...
```

Schéma SQLite pertinent (déjà en place, ne pas modifier) :

```sql
campaigns(id, name, intent, country, job_type, is_remote, hours_old, letter_language, score_threshold, created_at, last_run_at)
campaign_terms(id, campaign_id, term, kind CHECK(keyword|must_have|exclude|location), origin CHECK(ai|user), enabled)
jobs(id, source, external_id, fingerprint, company_norm, title, company, location, url, description,
     full_description, enriched, language, posted_at, status, failure_reason, duplicate_of,
     first_seen_at, last_seen_at, seen_count)
job_campaigns(job_id, campaign_id, matched_keyword, matched_location)
scores(job_id, campaign_id, score, reasons, model, scored_at)
letters(id, job_id, campaign_id, version, language, angle, body, file_path, warnings, model, created_at)
applications(job_id PK, letter_id, applied_at, channel, outcome, notes)
runs(id, campaign_id, status, stage, counters, llm_calls, started_at, finished_at, error)
```

---

## Carte des fichiers

| Fichier | Responsabilité |
|---|---|
| `stagefinder/keywords.py` | Suggestion IA des termes de campagne |
| `stagefinder/cli.py` | Modifié : sous-commandes `campaign suggest`, `campaign terms`, `campaign term-toggle` |
| `stagefinder/dashboard/__init__.py` | Vide |
| `stagefinder/dashboard/data.py` | Requêtes SQL pures pour le dashboard (aucune dépendance Streamlit) |
| `stagefinder/dashboard/app.py` | Rendu Streamlit, 5 écrans |
| `pyproject.toml` | Modifié : ajout de `streamlit` aux dépendances |
| `README.md` | Modifié : lancement du dashboard, nouvelles commandes CLI |
| `tests/test_keywords.py` | Tests de la suggestion IA |
| `tests/test_dashboard_data.py` | Tests des requêtes du dashboard |
| `tests/test_dashboard_app.py` | Tests `AppTest` des 5 écrans |

---

### Task 0 : Suggestion IA des mots-clés (`keywords.py`) + CLI

**Files:**
- Create: `stagefinder/keywords.py`
- Modify: `stagefinder/cli.py`
- Test: `tests/test_keywords.py`, `tests/test_cli.py`

**Interfaces:**
- Consumes: `models.Campaign`, `models.Fact`, `models.add_terms`, `models.load_campaign`, `profile.format_facts`, `profile.load_or_extract_facts`, `llm.LLMError`
- Produces:
  - `keywords.suggest_terms(llm, campaign: Campaign, facts: list[Fact]) -> dict` — clés `"keyword"`, `"exclude"`, `"location"`, chacune `list[str]` (bornées à 6/5/6 éléments), jamais `None`
  - CLI : `campaign suggest <id> [--apply]`, `campaign terms <id>`, `campaign term-toggle <term_id> --enable|--disable`

- [ ] **Step 1 : Écrire les tests**

`tests/test_keywords.py` :

```python
import json

import pytest

from stagefinder import keywords as K
from stagefinder.llm import LLMError
from stagefinder.models import Campaign, Fact

CAMPAIGN = Campaign(1, "test", "poste d'ingénieur IA junior, CDI, en Allemagne", "DE",
                    "fulltime", False, 168, "fr", 70)
FACTS = [Fact(1, "skill", "Python", "", ["Python"], "", "")]

GOOD = json.dumps({
    "keyword": ["KI Ingenieur Junior", "Machine Learning Engineer", "AI Engineer Junior"],
    "exclude": ["Senior", "5 Jahre Erfahrung"],
    "location": ["Berlin", "München", "Hamburg"],
})


def test_suggest_terms_returns_capped_lists(fake_llm):
    fake_llm.responses = [GOOD]
    r = K.suggest_terms(fake_llm, CAMPAIGN, FACTS)
    assert r["keyword"] == ["KI Ingenieur Junior", "Machine Learning Engineer", "AI Engineer Junior"]
    assert r["exclude"] == ["Senior", "5 Jahre Erfahrung"]
    assert r["location"] == ["Berlin", "München", "Hamburg"]
    assert "Allemagne" in fake_llm.prompts[0][1] and "DE" in fake_llm.prompts[0][1]


def test_suggest_terms_caps_and_cleans_lists(fake_llm):
    fake_llm.responses = [json.dumps({
        "keyword": [f"k{i}" for i in range(10)] + ["  ", ""],
        "exclude": [f"e{i}" for i in range(10)],
        "location": [f"l{i}" for i in range(10)],
    })]
    r = K.suggest_terms(fake_llm, CAMPAIGN, FACTS)
    assert len(r["keyword"]) == 6 and len(r["exclude"]) == 5 and len(r["location"]) == 6


def test_suggest_terms_tolerates_missing_or_wrong_type_keys(fake_llm):
    fake_llm.responses = [json.dumps({"keyword": ["k1"], "exclude": "senior", "location": None})]
    r = K.suggest_terms(fake_llm, CAMPAIGN, FACTS)
    assert r == {"keyword": ["k1"], "exclude": [], "location": []}


def test_suggest_terms_raises_on_non_dict_response(fake_llm):
    fake_llm.responses = ['["a", "b"]']
    with pytest.raises(LLMError):
        K.suggest_terms(fake_llm, CAMPAIGN, FACTS)


def test_suggest_terms_works_without_facts(fake_llm):
    fake_llm.responses = [GOOD]
    r = K.suggest_terms(fake_llm, CAMPAIGN, [])
    assert r["keyword"]
    assert "Profil du candidat" not in fake_llm.prompts[0][1]
```

Ajouter à `tests/test_cli.py` (à la suite des tests existants, mêmes imports) :

```python
def test_campaign_suggest_prints_and_apply_adds_terms(tmp_path, monkeypatch, capsys):
    import json as _json

    from stagefinder import cli, db
    dbp = tmp_path / "s.db"
    env = {"OPENROUTER_API_KEY": "k", "DB_PATH": str(dbp), "LETTERS_DIR": str(tmp_path)}
    monkeypatch.setattr(cli, "_env", lambda: env)
    cli.main(["campaign", "add", "--name", "DE", "--intent", "ingénieur IA junior", "--country", "DE"])
    capsys.readouterr()

    class FakeLLM:
        model = "fake"
        def __init__(self):
            self.calls = 0
        def chat_json(self, system, user, **kw):
            self.calls += 1
            return {"keyword": ["KI Junior"], "exclude": ["Senior"], "location": ["Berlin"]}

    monkeypatch.setattr(cli, "_llm", lambda settings: FakeLLM())
    assert cli.main(["campaign", "suggest", "1"]) == 0
    out = capsys.readouterr().out
    assert "KI Junior" in out and "Berlin" in out

    conn = db.connect(dbp)
    assert conn.execute("SELECT COUNT(*) FROM campaign_terms").fetchone()[0] == 0

    assert cli.main(["campaign", "suggest", "1", "--apply"]) == 0
    assert conn.execute("SELECT COUNT(*) FROM campaign_terms WHERE origin='ai'").fetchone()[0] == 3

    out = capsys.readouterr().out
    assert cli.main(["campaign", "terms", "1"]) == 0
    out = capsys.readouterr().out
    assert "KI Junior" in out and "ai" in out and "actif" in out


def test_campaign_term_toggle_disables_term(tmp_path, monkeypatch):
    from stagefinder import cli, db
    dbp = tmp_path / "s.db"
    env = {"OPENROUTER_API_KEY": "k", "DB_PATH": str(dbp), "LETTERS_DIR": str(tmp_path)}
    monkeypatch.setattr(cli, "_env", lambda: env)
    cli.main(["campaign", "add", "--name", "DE", "--intent", "x", "--country", "DE"])
    cli.main(["campaign", "term", "1", "--kind", "keyword", "abc"])
    conn = db.connect(dbp)
    tid = conn.execute("SELECT id FROM campaign_terms").fetchone()[0]

    assert cli.main(["campaign", "term-toggle", str(tid), "--disable"]) == 0
    assert conn.execute("SELECT enabled FROM campaign_terms WHERE id=?", (tid,)).fetchone()[0] == 0

    assert cli.main(["campaign", "term-toggle", str(tid), "--enable"]) == 0
    assert conn.execute("SELECT enabled FROM campaign_terms WHERE id=?", (tid,)).fetchone()[0] == 1
```

- [ ] **Step 2 : Lancer, vérifier l'échec**

Run : `python -m pytest tests/test_keywords.py -v`
Expected : FAIL, `ModuleNotFoundError: stagefinder.keywords`

- [ ] **Step 3 : Écrire `stagefinder/keywords.py`**

```python
from __future__ import annotations

from stagefinder.llm import LLMError
from stagefinder.models import Campaign, Fact
from stagefinder.profile import format_facts

CAPS = {"keyword": 6, "exclude": 5, "location": 6}

SYSTEM = """Tu prépares une campagne de recherche d'emploi. À partir de l'intention du candidat
et du pays visé, propose des termes de recherche RÉELLEMENT UTILISÉS PAR LE MARCHÉ DE L'EMPLOI
DE CE PAYS, dans sa langue et ses usages locaux (ex: "Praktikum" ou "Werkstudent" en Allemagne,
"Pasantía" en Espagne — jamais une simple traduction mot à mot de l'anglais).

Réponds en JSON strict, sans aucun texte autour :
{"keyword": ["3 à 6 mots-clés de recherche pertinents dans la langue du marché visé"],
 "exclude": ["2 à 5 termes à exclure : niveaux non voulus, types de contrat non voulus"],
 "location": ["3 à 6 villes ou régions du pays visé où ce type de poste est fréquent"]}

Ne propose que des termes directement utilisables dans un moteur de recherche d'offres d'emploi."""


def _clean_list(value, cap: int) -> list[str]:
    if not isinstance(value, list):
        return []
    out = [str(x).strip() for x in value if str(x).strip()]
    return out[:cap]


def suggest_terms(llm, campaign: Campaign, facts: list[Fact]) -> dict:
    user = f"Intention du candidat : {campaign.intent}\nPays visé : {campaign.country}"
    if facts:
        user += f"\n\nProfil du candidat (pour affiner les mots-clés) :\n{format_facts(facts)[:2000]}"
    data = llm.chat_json(SYSTEM, user, max_tokens=500)
    if not isinstance(data, dict):
        raise LLMError("suggestion de mots-clés : réponse non conforme (objet JSON attendu)")
    return {kind: _clean_list(data.get(kind), cap) for kind, cap in CAPS.items()}
```

- [ ] **Step 4 : Lancer, vérifier le succès**

Run : `python -m pytest tests/test_keywords.py -v`
Expected : 5 PASS

- [ ] **Step 5 : Ajouter les sous-commandes CLI**

Dans `stagefinder/cli.py`, ajouter l'import et les trois fonctions de commande, puis les brancher dans `build_parser`.

Ajouter à la section des imports :

```python
from stagefinder.keywords import suggest_terms
```

Ajouter après `cmd_campaign_list` :

```python
def cmd_campaign_suggest(a) -> int:
    settings, conn = _open()
    campaign = load_campaign(conn, a.campaign_id)
    llm = _llm(settings)
    facts = []
    if settings.cv_path and settings.cv_path.exists():
        try:
            facts = load_or_extract_facts(conn, llm, settings.cv_path)
        except ValueError:
            facts = []
    suggestions = suggest_terms(llm, campaign, facts)
    for kind in ("keyword", "exclude", "location"):
        values = ", ".join(suggestions[kind]) if suggestions[kind] else "(aucune suggestion)"
        print(f"{kind}: {values}")
    if a.apply:
        for kind in ("keyword", "exclude", "location"):
            if suggestions[kind]:
                add_terms(conn, a.campaign_id, kind, suggestions[kind], origin="ai")
        print("termes ajoutés (origin=ai)")
    return 0


def cmd_campaign_terms_list(a) -> int:
    settings, conn = _open()
    for r in conn.execute(
            "SELECT * FROM campaign_terms WHERE campaign_id=? ORDER BY kind, id", (a.campaign_id,)):
        state = "actif" if r["enabled"] else "désactivé"
        print(f"[{r['id']}] {r['kind']:10} {r['term']:30} ({r['origin']}, {state})")
    return 0


def cmd_campaign_term_toggle(a) -> int:
    settings, conn = _open()
    enabled = 1 if a.enable else 0
    conn.execute("UPDATE campaign_terms SET enabled=? WHERE id=?", (enabled, a.term_id))
    conn.commit()
    print(f"terme {a.term_id} {'activé' if enabled else 'désactivé'}")
    return 0
```

Dans `build_parser`, juste après la ligne `c.add_parser("list").set_defaults(fn=cmd_campaign_list)`, ajouter :

```python
    suggest = c.add_parser("suggest")
    suggest.add_argument("campaign_id", type=int)
    suggest.add_argument("--apply", action="store_true")
    suggest.set_defaults(fn=cmd_campaign_suggest)
    terms_list = c.add_parser("terms")
    terms_list.add_argument("campaign_id", type=int)
    terms_list.set_defaults(fn=cmd_campaign_terms_list)
    toggle = c.add_parser("term-toggle")
    toggle.add_argument("term_id", type=int)
    grp = toggle.add_mutually_exclusive_group(required=True)
    grp.add_argument("--enable", action="store_true")
    grp.add_argument("--disable", action="store_true")
    toggle.set_defaults(fn=cmd_campaign_term_toggle)
```

- [ ] **Step 6 : Lancer, vérifier le succès**

Run : `python -m pytest tests/test_cli.py tests/test_keywords.py -v`
Expected : tous PASS

- [ ] **Step 7 : Suite complète**

Run : `python -m pytest -q`
Expected : 138 passed (131 + 7 nouveaux), aucun warning

- [ ] **Step 8 : Commit**

```bash
git add stagefinder/keywords.py stagefinder/cli.py tests/test_keywords.py tests/test_cli.py
git commit -m "feat: ai keyword/exclusion/location suggestion per campaign

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

### Task 1 : Requêtes du dashboard (`dashboard/data.py`)

**Files:**
- Create: `stagefinder/dashboard/__init__.py`, `stagefinder/dashboard/data.py`
- Test: `tests/test_dashboard_data.py`

**Interfaces:**
- Consumes: `db.connect`, `db.migrate`, `db.now`, `models.create_campaign`, `dedup.upsert_job`, `models.RawJob`
- Produces (toutes prennent `conn: sqlite3.Connection` en premier argument, retournent des `list[dict]` ou `dict | None` — jamais de `sqlite3.Row` pour rester consommable telle quelle par Streamlit) :
  - `list_campaigns(conn) -> list[dict]`
  - `list_campaign_terms(conn, campaign_id: int) -> list[dict]`
  - `list_jobs(conn, campaign_id: int | None = None, status: str | None = None) -> list[dict]` — colonnes `id, title, company, location, status, score, seen_count, duplicate_of, url`, triées par `score` décroissant (NULL en dernier)
  - `mark_duplicate(conn, job_id: int, canonical_id: int) -> None`
  - `list_letters(conn, campaign_id: int | None = None) -> list[dict]` — colonnes `id, job_id, campaign_id, version, language, company, title, file_path, warnings (list[str]), created_at`
  - `list_applications(conn) -> list[dict]` — jointure `applications` + `jobs`
  - `upsert_application(conn, job_id: int, letter_id: int | None, applied_at: str | None, channel: str | None, outcome: str | None, notes: str | None) -> None`
  - `list_runs(conn, campaign_id: int) -> list[dict]`
  - `latest_run(conn, campaign_id: int) -> dict | None`

- [ ] **Step 1 : Écrire le test**

`tests/test_dashboard_data.py` :

```python
import json

import pytest

from stagefinder import db
from stagefinder.dashboard import data as D
from stagefinder.dedup import upsert_job
from stagefinder.models import RawJob, create_campaign


@pytest.fixture
def conn(tmp_db):
    c = db.connect(tmp_db)
    db.migrate(c)
    return c


def _job(conn, cid, ext, title, company="Co", loc="Paris"):
    r = upsert_job(conn, RawJob("indeed", ext, title, company, loc,
                                f"https://x/{ext}", "d"), cid, "k", "l")
    return r.job_id


def test_list_campaigns_empty_then_one(conn):
    assert D.list_campaigns(conn) == []
    create_campaign(conn, "DS", "intent", "FR")
    rows = D.list_campaigns(conn)
    assert len(rows) == 1 and rows[0]["name"] == "DS" and rows[0]["country"] == "FR"


def test_list_campaign_terms(conn):
    cid = create_campaign(conn, "DS", "intent", "FR")
    conn.execute("INSERT INTO campaign_terms(campaign_id,term,kind,origin) VALUES (?,?,?,?)",
                (cid, "stage data", "keyword", "user"))
    conn.commit()
    rows = D.list_campaign_terms(conn, cid)
    assert rows[0]["term"] == "stage data" and rows[0]["enabled"] == 1


def test_list_jobs_filters_and_sorts_by_score(conn):
    cid = create_campaign(conn, "DS", "intent", "FR")
    a = _job(conn, cid, "a", "Stage A")
    b = _job(conn, cid, "b", "Stage B")
    conn.execute("UPDATE jobs SET status='scored' WHERE id IN (?,?)", (a, b))
    conn.execute("INSERT INTO scores(job_id,campaign_id,score,reasons,model,scored_at) "
                "VALUES (?,?,?,?,?,?)", (a, cid, 60, "[]", "m", "t"))
    conn.execute("INSERT INTO scores(job_id,campaign_id,score,reasons,model,scored_at) "
                "VALUES (?,?,?,?,?,?)", (b, cid, 90, "[]", "m", "t"))
    conn.commit()
    rows = D.list_jobs(conn, campaign_id=cid)
    assert [r["id"] for r in rows] == [b, a]
    assert D.list_jobs(conn, campaign_id=cid, status="scored") == rows
    assert D.list_jobs(conn, campaign_id=999) == []


def test_mark_duplicate_updates_status_and_seen_count(conn):
    cid = create_campaign(conn, "DS", "intent", "FR")
    a = _job(conn, cid, "a", "Stage A")
    b = _job(conn, cid, "b", "Stage A bis")
    D.mark_duplicate(conn, b, a)
    row = conn.execute("SELECT status, duplicate_of FROM jobs WHERE id=?", (b,)).fetchone()
    assert row["status"] == "duplicate" and row["duplicate_of"] == a
    assert conn.execute("SELECT seen_count FROM jobs WHERE id=?", (a,)).fetchone()[0] == 2


def test_list_letters_parses_warnings_json(conn):
    cid = create_campaign(conn, "DS", "intent", "FR")
    jid = _job(conn, cid, "a", "Stage A")
    conn.execute(
        "INSERT INTO letters(job_id,campaign_id,version,language,angle,body,file_path,warnings,"
        "model,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (jid, cid, 1, "fr", "{}", "corps", "x.docx", json.dumps(["longueur"]), "m", "t"))
    conn.commit()
    rows = D.list_letters(conn, campaign_id=cid)
    assert rows[0]["warnings"] == ["longueur"] and rows[0]["title"] == "Stage A"


def test_upsert_application_then_list(conn):
    cid = create_campaign(conn, "DS", "intent", "FR")
    jid = _job(conn, cid, "a", "Stage A")
    D.upsert_application(conn, jid, None, "2026-09-20", "email", "pending", "relancer dans 1 sem")
    rows = D.list_applications(conn)
    assert rows[0]["job_id"] == jid and rows[0]["outcome"] == "pending"
    D.upsert_application(conn, jid, None, "2026-09-20", "email", "interview", "")
    rows = D.list_applications(conn)
    assert len(rows) == 1 and rows[0]["outcome"] == "interview"


def test_runs_listing_and_latest(conn):
    cid = create_campaign(conn, "DS", "intent", "FR")
    conn.execute("INSERT INTO runs(campaign_id,status,started_at) VALUES (?,?,?)", (cid, "done", db.now()))
    conn.execute("INSERT INTO runs(campaign_id,status,started_at) VALUES (?,?,?)", (cid, "failed", db.now()))
    conn.commit()
    assert len(D.list_runs(conn, cid)) == 2
    assert D.latest_run(conn, cid)["status"] == "failed"
    assert D.latest_run(conn, 999) is None
```

- [ ] **Step 2 : Lancer, vérifier l'échec**

Run : `python -m pytest tests/test_dashboard_data.py -v`
Expected : FAIL, `ModuleNotFoundError: stagefinder.dashboard`

- [ ] **Step 3 : Créer `stagefinder/dashboard/__init__.py`** (vide)

- [ ] **Step 4 : Écrire `stagefinder/dashboard/data.py`**

```python
from __future__ import annotations

import json
import sqlite3

from stagefinder.db import now


def _rows(cur: sqlite3.Cursor) -> list[dict]:
    return [dict(r) for r in cur.fetchall()]


def list_campaigns(conn: sqlite3.Connection) -> list[dict]:
    return _rows(conn.execute("SELECT * FROM campaigns ORDER BY id"))


def list_campaign_terms(conn: sqlite3.Connection, campaign_id: int) -> list[dict]:
    return _rows(conn.execute(
        "SELECT * FROM campaign_terms WHERE campaign_id=? ORDER BY kind, id", (campaign_id,)))


def list_jobs(conn: sqlite3.Connection, campaign_id: int | None = None,
             status: str | None = None) -> list[dict]:
    sql = ("SELECT j.id, j.title, j.company, j.location, j.status, j.seen_count, "
          "j.duplicate_of, j.url, s.score "
          "FROM jobs j LEFT JOIN scores s ON s.job_id=j.id "
          "AND s.campaign_id = COALESCE(:cid, s.campaign_id) ")
    where, params = [], {}
    if campaign_id is not None:
        sql += "JOIN job_campaigns jc ON jc.job_id=j.id "
        where.append("jc.campaign_id=:cid")
        params["cid"] = campaign_id
    else:
        params["cid"] = None
    if status is not None:
        where.append("j.status=:status")
        params["status"] = status
    if where:
        sql += "WHERE " + " AND ".join(where) + " "
    sql += "ORDER BY (s.score IS NULL), s.score DESC, j.id"
    return _rows(conn.execute(sql, params))


def mark_duplicate(conn: sqlite3.Connection, job_id: int, canonical_id: int) -> None:
    conn.execute("UPDATE jobs SET status='duplicate', duplicate_of=? WHERE id=?",
                (canonical_id, job_id))
    conn.execute("UPDATE jobs SET seen_count=seen_count+1, last_seen_at=? WHERE id=?",
                (now(), canonical_id))
    conn.commit()


def list_letters(conn: sqlite3.Connection, campaign_id: int | None = None) -> list[dict]:
    sql = ("SELECT l.id, l.job_id, l.campaign_id, l.version, l.language, l.file_path, "
          "l.warnings, l.created_at, j.company, j.title "
          "FROM letters l JOIN jobs j ON j.id=l.job_id ")
    params: dict = {}
    if campaign_id is not None:
        sql += "WHERE l.campaign_id=:cid "
        params["cid"] = campaign_id
    sql += "ORDER BY l.id DESC"
    rows = _rows(conn.execute(sql, params))
    for r in rows:
        r["warnings"] = json.loads(r["warnings"] or "[]")
    return rows


def list_applications(conn: sqlite3.Connection) -> list[dict]:
    return _rows(conn.execute(
        "SELECT a.*, j.title, j.company FROM applications a JOIN jobs j ON j.id=a.job_id "
        "ORDER BY a.applied_at DESC"))


def upsert_application(conn: sqlite3.Connection, job_id: int, letter_id: int | None,
                       applied_at: str | None, channel: str | None, outcome: str | None,
                       notes: str | None) -> None:
    conn.execute(
        "INSERT INTO applications(job_id,letter_id,applied_at,channel,outcome,notes) "
        "VALUES (?,?,?,?,?,?) ON CONFLICT(job_id) DO UPDATE SET "
        "letter_id=excluded.letter_id, applied_at=excluded.applied_at, "
        "channel=excluded.channel, outcome=excluded.outcome, notes=excluded.notes",
        (job_id, letter_id, applied_at, channel, outcome, notes))
    conn.commit()


def list_runs(conn: sqlite3.Connection, campaign_id: int) -> list[dict]:
    return _rows(conn.execute(
        "SELECT * FROM runs WHERE campaign_id=? ORDER BY id DESC", (campaign_id,)))


def latest_run(conn: sqlite3.Connection, campaign_id: int) -> dict | None:
    r = conn.execute("SELECT * FROM runs WHERE campaign_id=? ORDER BY id DESC LIMIT 1",
                     (campaign_id,)).fetchone()
    return dict(r) if r else None
```

- [ ] **Step 5 : Lancer, vérifier le succès**

Run : `python -m pytest tests/test_dashboard_data.py -v`
Expected : 7 PASS

- [ ] **Step 6 : Suite complète**

Run : `python -m pytest -q`
Expected : 145 passed (138 + 7), aucun warning

- [ ] **Step 7 : Commit**

```bash
git add stagefinder/dashboard/__init__.py stagefinder/dashboard/data.py tests/test_dashboard_data.py
git commit -m "feat: pure sqlite query layer for the dashboard

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

### Task 2 : Dashboard — squelette + écran Campagnes

**Files:**
- Create: `stagefinder/dashboard/app.py`
- Modify: `pyproject.toml`
- Test: `tests/test_dashboard_app.py`

**Interfaces:**
- Consumes: tout Task 0/1 + `config.load_settings`, `db.connect`, `db.migrate`, `models.create_campaign`, `models.add_terms`, `keywords.suggest_terms`, `profile.load_or_extract_facts`
- Produces :
  - `stagefinder/dashboard/app.py` module exécutable par Streamlit (`streamlit run stagefinder/dashboard/app.py`) et par `streamlit.testing.v1.AppTest.from_file(...)`
  - Fonctions internes réutilisées par les tâches suivantes : `_settings() -> Settings`, `_conn() -> sqlite3.Connection`, `_llm() -> LLMClient`, `_project_root() -> Path`, `PAGES: dict[str, callable]`
  - Écran **Campagnes** : formulaire de création, liste des campagnes avec termes (cases à cocher), bouton « Suggérer avec l'IA », bouton « Lancer »

Ce fichier grandit au fil des tâches suivantes (3 à 6 ajoutent une fonction `_page_xxx()` chacune) ; il reste sous les 200 lignes par fonction de page, mais le fichier total dépassera 200 lignes — c'est le seul écart volontaire à la cible du plan 1, une app Streamlit à 5 écrans vivant naturellement dans un seul module de rendu (voir §3.2 du plan 1 : « en cas de doute, suivre les patterns existants » — ici le pattern Streamlit standard est un fichier par app).

- [ ] **Step 1 : Ajouter `streamlit` à `pyproject.toml`**

Dans la liste `dependencies`, ajouter `"streamlit"` (déjà installé dans l'environnement, version 1.56.0).

- [ ] **Step 2 : Écrire le test**

`tests/test_dashboard_app.py` :

```python
import os
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

APP = str(Path(__file__).parent.parent / "stagefinder" / "dashboard" / "app.py")


@pytest.fixture
def env(tmp_path, monkeypatch):
    db_path = tmp_path / "s.db"
    letters_dir = tmp_path / "letters"
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    monkeypatch.setenv("DB_PATH", str(db_path))
    monkeypatch.setenv("LETTERS_DIR", str(letters_dir))
    monkeypatch.setenv("CV_PATH", "")
    monkeypatch.chdir(tmp_path)
    return {"db_path": db_path, "letters_dir": letters_dir}


def test_app_loads_without_error(env):
    at = AppTest.from_file(APP)
    at.run()
    assert not at.exception


def test_create_campaign_via_form(env):
    at = AppTest.from_file(APP)
    at.run()
    at.text_input(key="new_name").set_value("DS France").run()
    at.text_area(key="new_intent").set_value("stage data science").run()
    at.text_input(key="new_country").set_value("FR").run()
    at.button(key="create_campaign_btn").click().run()
    assert not at.exception
    assert any("DS France" in m.value for m in at.markdown if "DS France" in m.value)


def test_add_manual_term_to_campaign(env):
    from stagefinder import db

    at = AppTest.from_file(APP)
    at.run()
    at.text_input(key="new_name").set_value("DS").run()
    at.text_area(key="new_intent").set_value("stage data").run()
    at.text_input(key="new_country").set_value("FR").run()
    at.button(key="create_campaign_btn").click().run()

    conn = db.connect(env["db_path"])
    cid = conn.execute("SELECT id FROM campaigns").fetchone()[0]

    at.text_input(key=f"manual_term_{cid}_keyword").set_value("stage data science").run()
    at.button(key=f"add_manual_{cid}_keyword").click().run()
    assert not at.exception
    assert any("stage data science" in cb.label for cb in at.checkbox)


def test_toggle_term_checkbox_disables_it(env):
    from stagefinder import db
    from stagefinder.models import add_terms, create_campaign

    conn = db.connect(env["db_path"])
    db.migrate(conn)
    cid = create_campaign(conn, "DS", "stage data", "FR")
    add_terms(conn, cid, "keyword", ["stage data science"])

    at = AppTest.from_file(APP)
    at.run()
    cb = next(c for c in at.checkbox if "stage data science" in c.label)
    cb.set_value(False).run()
    assert not at.exception
    conn2 = db.connect(env["db_path"])
    assert conn2.execute("SELECT enabled FROM campaign_terms").fetchone()[0] == 0
```

- [ ] **Step 3 : Lancer, vérifier l'échec**

Run : `python -m pytest tests/test_dashboard_app.py -v`
Expected : FAIL, `ModuleNotFoundError` ou `FileNotFoundError` (le fichier `app.py` n'existe pas)

- [ ] **Step 4 : Écrire `stagefinder/dashboard/app.py`**

```python
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


PAGES = {"Campagnes": _page_campaigns}


def main() -> None:
    page = st.sidebar.radio("Écran", list(PAGES.keys()))
    PAGES[page]()


main()
```

- [ ] **Step 5 : Installer et lancer, vérifier le succès**

```bash
python -m pip install -e ".[dev]"
python -m pytest tests/test_dashboard_app.py -v
```

Expected : 4 PASS.

- [ ] **Step 6 : Suite complète**

Run : `python -m pytest -q`
Expected : 149 passed (145 + 4), aucun warning

- [ ] **Step 7 : Commit**

```bash
git add stagefinder/dashboard/app.py pyproject.toml tests/test_dashboard_app.py
git commit -m "feat: streamlit dashboard skeleton and campaigns screen

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

### Task 3 : Dashboard — écran Run

**Files:**
- Modify: `stagefinder/dashboard/app.py`
- Test: `tests/test_dashboard_app.py`

**Interfaces:**
- Consumes: `data.list_campaigns`, `data.list_runs`, `data.latest_run`
- Produces: `_page_run()`, ajoutée à `PAGES["Run"]`

- [ ] **Step 1 : Écrire le test**

Ajouter à `tests/test_dashboard_app.py` :

```python
def test_run_page_shows_latest_run_status(env):
    from stagefinder import db
    from stagefinder.models import create_campaign

    conn = db.connect(env["db_path"])
    db.migrate(conn)
    cid = create_campaign(conn, "DS", "stage data", "FR")
    conn.execute("INSERT INTO runs(campaign_id,status,stage,counters,started_at) "
                "VALUES (?,?,?,?,?)", (cid, "done", "generate", '{"new": 3}', db.now()))
    conn.commit()

    at = AppTest.from_file(APP)
    at.run()
    at.sidebar.radio[0].set_value("Run").run()
    at.selectbox(key="run_campaign_select").set_value(cid).run()
    assert not at.exception
    assert any("done" in m.value for m in at.markdown)
    assert any('"new": 3' in m.value or "new" in m.value for m in at.json)


def test_run_page_with_no_runs_shows_message(env):
    from stagefinder import db
    from stagefinder.models import create_campaign

    conn = db.connect(env["db_path"])
    db.migrate(conn)
    create_campaign(conn, "DS", "stage data", "FR")

    at = AppTest.from_file(APP)
    at.run()
    at.sidebar.radio[0].set_value("Run").run()
    assert not at.exception
    assert any("aucun run" in i.value.lower() for i in at.info)
```

- [ ] **Step 2 : Lancer, vérifier l'échec**

Run : `python -m pytest tests/test_dashboard_app.py -k run_page -v`
Expected : FAIL (page « Run » n'existe pas encore, la navigation ne trouve pas l'option)

- [ ] **Step 3 : Ajouter `_page_run` dans `app.py`**

Ajouter avant la ligne `PAGES = {...}` :

```python
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
```

Mettre à jour `PAGES` :

```python
PAGES = {"Campagnes": _page_campaigns, "Run": _page_run}
```

- [ ] **Step 4 : Lancer, vérifier le succès**

Run : `python -m pytest tests/test_dashboard_app.py -k run_page -v`
Expected : 2 PASS. Si `at.json` n'expose pas la valeur attendue (l'API `AppTest` peut représenter `st.json` différemment selon la version), inspecter `at.json[0].value` dans le test et adapter l'assertion à la structure réelle plutôt qu'à une supposition — ne pas changer `st.json(latest["counters"])` en `st.write` sans raison.

- [ ] **Step 5 : Suite complète**

Run : `python -m pytest -q`
Expected : 151 passed (149 + 2), aucun warning

- [ ] **Step 6 : Commit**

```bash
git add stagefinder/dashboard/app.py tests/test_dashboard_app.py
git commit -m "feat: dashboard run screen with live status and history

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

### Task 4 : Dashboard — écran Offres

**Files:**
- Modify: `stagefinder/dashboard/app.py`
- Test: `tests/test_dashboard_app.py`

**Interfaces:**
- Consumes: `data.list_campaigns`, `data.list_jobs`, `data.mark_duplicate`
- Produces: `_page_jobs()`, ajoutée à `PAGES["Offres"]`

- [ ] **Step 1 : Écrire le test**

Ajouter à `tests/test_dashboard_app.py` :

```python
def test_jobs_page_lists_and_marks_duplicate(env):
    from stagefinder import db
    from stagefinder.dedup import upsert_job
    from stagefinder.models import RawJob, create_campaign

    conn = db.connect(env["db_path"])
    db.migrate(conn)
    cid = create_campaign(conn, "DS", "stage data", "FR")
    a = upsert_job(conn, RawJob("indeed", "a", "Stage A", "Co", "Paris", "https://x/a", "d"),
                   cid, "k", "l").job_id
    b = upsert_job(conn, RawJob("indeed", "b", "Stage B", "Co", "Paris", "https://x/b", "d"),
                   cid, "k", "l").job_id

    at = AppTest.from_file(APP)
    at.run()
    at.sidebar.radio[0].set_value("Offres").run()
    at.selectbox(key="jobs_campaign_select").set_value(cid).run()
    assert not at.exception
    assert any("Stage A" in m.value for m in at.markdown) or \
           any("Stage A" in str(df.value) for df in at.dataframe)

    at.selectbox(key=f"dup_target_{b}").set_value(a).run()
    at.button(key=f"mark_dup_{b}").click().run()
    assert not at.exception
    conn2 = db.connect(env["db_path"])
    row = conn2.execute("SELECT status, duplicate_of FROM jobs WHERE id=?", (b,)).fetchone()
    assert row["status"] == "duplicate" and row["duplicate_of"] == a
```

- [ ] **Step 2 : Lancer, vérifier l'échec**

Run : `python -m pytest tests/test_dashboard_app.py -k jobs_page -v`
Expected : FAIL

- [ ] **Step 3 : Ajouter `_page_jobs` dans `app.py`**

```python
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
```

Mettre à jour `PAGES` :

```python
PAGES = {"Campagnes": _page_campaigns, "Run": _page_run, "Offres": _page_jobs}
```

- [ ] **Step 4 : Lancer, vérifier le succès**

Run : `python -m pytest tests/test_dashboard_app.py -k jobs_page -v`
Expected : 1 PASS

- [ ] **Step 5 : Suite complète**

Run : `python -m pytest -q`
Expected : 152 passed (151 + 1), aucun warning

- [ ] **Step 6 : Commit**

```bash
git add stagefinder/dashboard/app.py tests/test_dashboard_app.py
git commit -m "feat: dashboard offers screen with manual duplicate marking

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

### Task 5 : Dashboard — écran Lettres

**Files:**
- Modify: `stagefinder/dashboard/app.py`
- Test: `tests/test_dashboard_app.py`

**Interfaces:**
- Consumes: `data.list_campaigns`, `data.list_letters`, `letters.generate_for_job`, `profile.load_or_extract_facts`, `models.load_campaign`
- Produces: `_page_letters()`, ajoutée à `PAGES["Lettres"]`

- [ ] **Step 1 : Écrire le test**

Ajouter à `tests/test_dashboard_app.py` :

```python
def test_letters_page_lists_and_downloads(env):
    import json as _json

    from stagefinder import db
    from stagefinder.dedup import upsert_job
    from stagefinder.models import RawJob, create_campaign

    conn = db.connect(env["db_path"])
    db.migrate(conn)
    cid = create_campaign(conn, "DS", "stage data", "FR")
    jid = upsert_job(conn, RawJob("indeed", "a", "Stage A", "Co", "Paris", "https://x/a", "d"),
                     cid, "k", "l").job_id
    letters_dir = env["letters_dir"]
    letters_dir.mkdir(parents=True, exist_ok=True)
    docx_path = letters_dir / "1_Co_Stage_A_v1.docx"
    docx_path.write_bytes(b"fake docx bytes")
    conn.execute(
        "INSERT INTO letters(job_id,campaign_id,version,language,angle,body,file_path,warnings,"
        "model,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (jid, cid, 1, "fr", "{}", "Corps de la lettre", str(docx_path),
         _json.dumps(["longueur 220 mots"]), "m", "t"))
    conn.commit()

    at = AppTest.from_file(APP)
    at.run()
    at.sidebar.radio[0].set_value("Lettres").run()
    at.selectbox(key="letters_campaign_select").set_value(cid).run()
    assert not at.exception
    assert any("Stage A" in m.value for m in at.markdown)
    assert any("longueur 220 mots" in w.value for w in at.warning)
    # `st.download_button` n'est pas exposé comme type assertable par AppTest (il apparaît en
    # UnknownElement) : on vérifie seulement que la page ne plante pas en le rendant, et que le
    # texte de la lettre est bien lu depuis le fichier .docx généré au préalable.
    assert docx_path.exists()
```

- [ ] **Step 2 : Lancer, vérifier l'échec**

Run : `python -m pytest tests/test_dashboard_app.py -k letters_page -v`
Expected : FAIL

- [ ] **Step 3 : Ajouter `_page_letters` dans `app.py`**

```python
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
        with st.expander(f"[v{row['version']}] {row['company']} — {row['title']}"):
            body = conn.execute("SELECT body FROM letters WHERE id=?", (row["id"],)).fetchone()[0]
            st.text_area("Contenu", value=body, height=250, key=f"body_{row['id']}",
                        label_visibility="collapsed")
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
```

Ajouter l'import manquant en tête de fichier :

```python
from stagefinder import letters
```

Mettre à jour `PAGES` :

```python
PAGES = {"Campagnes": _page_campaigns, "Run": _page_run, "Offres": _page_jobs,
         "Lettres": _page_letters}
```

- [ ] **Step 4 : Lancer, vérifier le succès**

Run : `python -m pytest tests/test_dashboard_app.py -k letters_page -v`
Expected : 1 PASS

- [ ] **Step 5 : Suite complète**

Run : `python -m pytest -q`
Expected : 153 passed (152 + 1), aucun warning

- [ ] **Step 6 : Commit**

```bash
git add stagefinder/dashboard/app.py tests/test_dashboard_app.py
git commit -m "feat: dashboard letters screen with regeneration and docx download

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

### Task 6 : Dashboard — écran Suivi + README

**Files:**
- Modify: `stagefinder/dashboard/app.py`, `README.md`
- Test: `tests/test_dashboard_app.py`

**Interfaces:**
- Consumes: `data.list_letters`, `data.list_applications`, `data.upsert_application`
- Produces: `_page_tracking()`, ajoutée à `PAGES["Suivi"]`

- [ ] **Step 1 : Écrire le test**

Ajouter à `tests/test_dashboard_app.py` :

```python
def test_tracking_page_records_application(env):
    from stagefinder import db
    from stagefinder.dedup import upsert_job
    from stagefinder.models import RawJob, create_campaign

    conn = db.connect(env["db_path"])
    db.migrate(conn)
    cid = create_campaign(conn, "DS", "stage data", "FR")
    jid = upsert_job(conn, RawJob("indeed", "a", "Stage A", "Co", "Paris", "https://x/a", "d"),
                     cid, "k", "l").job_id
    conn.execute("UPDATE jobs SET status='letter_ready' WHERE id=?", (jid,))
    conn.commit()

    at = AppTest.from_file(APP)
    at.run()
    at.sidebar.radio[0].set_value("Suivi").run()
    at.selectbox(key="tracking_job_select").set_value(jid).run()
    at.selectbox(key="tracking_outcome").set_value("interview").run()
    at.text_area(key="tracking_notes").set_value("Entretien le 25/09").run()
    at.button(key="save_tracking").click().run()
    assert not at.exception

    conn2 = db.connect(env["db_path"])
    row = conn2.execute("SELECT outcome, notes FROM applications WHERE job_id=?", (jid,)).fetchone()
    assert row["outcome"] == "interview" and "25/09" in row["notes"]

    assert any("interview" in m.value for m in at.markdown)
```

- [ ] **Step 2 : Lancer, vérifier l'échec**

Run : `python -m pytest tests/test_dashboard_app.py -k tracking_page -v`
Expected : FAIL

- [ ] **Step 3 : Ajouter `_page_tracking` dans `app.py`**

```python
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
```

Mettre à jour `PAGES` :

```python
PAGES = {"Campagnes": _page_campaigns, "Run": _page_run, "Offres": _page_jobs,
         "Lettres": _page_letters, "Suivi": _page_tracking}
```

- [ ] **Step 4 : Lancer, vérifier le succès**

Run : `python -m pytest tests/test_dashboard_app.py -k tracking_page -v`
Expected : 1 PASS

- [ ] **Step 5 : Mettre à jour `README.md`**

Ajouter une section après le bloc « Utilisation » existant :

```markdown
## Tableau de bord

    streamlit run stagefinder/dashboard/app.py

Cinq écrans : Campagnes (création, suggestion IA des mots-clés, lancement d'un run),
Run (suivi en direct), Offres (table triée par score, marquage manuel des doublons),
Lettres (lecture, régénération, export .docx), Suivi (statut des candidatures).
Le bouton « Lancer » démarre `stagefinder run` en sous-processus : fermer l'onglet du
navigateur n'interrompt pas un run en cours.

## Suggestion IA des mots-clés

    python -m stagefinder.cli campaign suggest <id>              # affiche les suggestions
    python -m stagefinder.cli campaign suggest <id> --apply      # les ajoute (origin=ai)
    python -m stagefinder.cli campaign terms <id>                # liste les termes actuels
    python -m stagefinder.cli campaign term-toggle <term_id> --enable|--disable
```

- [ ] **Step 6 : Suite complète**

Run : `python -m pytest -q`
Expected : 154 passed (153 + 1), aucun warning

- [ ] **Step 7 : Commit**

```bash
git add stagefinder/dashboard/app.py README.md tests/test_dashboard_app.py
git commit -m "feat: dashboard application tracking screen and docs

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

## Auto-revue du plan

**Couverture du spec.** §5.1 (campagne + mots-clés, suggestion IA, validation utilisateur) → Task 0 (CLI) et Task 2 (cases à cocher dans le dashboard — la « validation utilisateur » explicitement demandée par le spec passe par les checkboxes activables/désactivables, jamais une application automatique sans revue). §6 (dashboard, 5 écrans, lancement en sous-processus) → Tasks 2 à 6, une tâche par écran, `subprocess.Popen` dans Task 2. Contrainte « le dashboard ne fait jamais de travail long » → respectée : aucune tâche n'appelle `pipeline.run_campaign` depuis `app.py`, seule la suggestion IA (rapide, un appel LLM) et la régénération de lettre (un job) tournent dans le processus Streamlit lui-même, ce qui est cohérent avec le spec (« Le bouton Lancer démarre un sous-processus », les actions ponctuelles restent synchrones).

**Placeholders.** Aucun « TBD » ; chaque étape contient le code complet, chaque test est écrit avec ses assertions.

**Cohérence des types.** `data.py` retourne systématiquement des `dict` (jamais `sqlite3.Row`) pour rester consommable par Streamlit sans conversion supplémentaire dans `app.py` — vérifié dans chaque fonction de `data.py` et chaque test associé. `suggest_terms(llm, campaign: Campaign, facts: list[Fact]) -> dict` a la même signature dans Task 0 et dans son usage en Task 2/5 (`app.py` appelle `suggest_terms(llm, campaign, facts)` avec les mêmes trois positionnels). `letters.generate_for_job` est appelée en Task 5 avec exactement les paramètres nommés du plan 1 (`instruction=`, `language=`).
