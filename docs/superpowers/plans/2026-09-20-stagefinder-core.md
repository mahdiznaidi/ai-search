# StageFinder v2 — Plan d'implémentation 1/2 : pipeline cœur

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Construire le pipeline complet (campagne → discover → dedup → enrich → score → lettres → suivi) sur SQLite, pilotable en CLI, idempotent, testé de bout en bout sans réseau.

**Architecture:** Une base SQLite est la source de vérité ; chaque étape est un module qui ne traite que les offres au statut précédent. Tous les appels LLM passent par `llm.py` ; toutes les I/O réseau sont injectables (fonction `fetch`, objets `Source`) pour que les tests tournent hors ligne. Le dashboard et la suggestion IA des mots-clés sont dans le plan 2/2.

**Tech Stack:** Python 3.11, sqlite3 (stdlib), python-jobspy 1.1.82, requests, beautifulsoup4, PyPDF2, rapidfuzz, langdetect, python-docx, python-dotenv, pytest.

**Spec:** `docs/superpowers/specs/2026-09-20-stagefinder-design.md`

## Global Constraints

- Rien de métier en dur dans le code : mots-clés, exclusions, villes, pays, type de poste, langue viennent de la table `campaigns` / `campaign_terms`.
- Aucun repli silencieux : un échec produit un statut `failed_*` + `failure_reason`, ou un `runs.status = failed`, jamais un résultat qui ressemble à un succès.
- Retry LLM uniquement sur 429 / 5xx / timeout, 3 tentatives, backoff exponentiel. Jamais sur 400/401/403.
- Plafond d'appels LLM par run : `MAX_LLM_CALLS_PER_RUN` (défaut 300).
- Secrets uniquement via `.env` / environnement. La clé en clair dans `stage_finder_with_ai.py` est à révoquer.
- Aucun appel réseau dans les tests.
- Cible < 200 lignes par module.
- Un second run identique ne crée aucun job, n'appelle pas le LLM, ne crée aucune lettre (test d'idempotence, Task 10).
- Messages de commit terminés par `Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>`.

## Écarts assumés par rapport au spec

- `jobs` gagne `company_norm TEXT` (indexée) pour la couche 3 de dédup (comparaison floue restreinte à la même entreprise) et `enriched TEXT` (JSON produit par l'enrichissement : missions, stack, contexte).
- Le gabarit `.docx` issu de `cover letter.docx` est reporté au plan 2 ; ici `render_docx` produit un document simple.
- `Source.search(...)` prend un paramètre `is_remote` supplémentaire (non prévu dans l'interface `Source` initiale du spec) pour transmettre le filtre remote de la campagne aux sources.
- La couche 3 de dédup utilise `fuzz.token_sort_ratio` plutôt que `fuzz.token_set_ratio` : `token_set_ratio` dégénère à 100 dès qu'un titre est un sur-ensemble de l'autre (ex. « Stage Data Analyst » vs « Stage Data Analyst Marketing »), ce qui fusionnait silencieusement des offres distinctes.
- L'enrichissement est « description-first » : si la description courte issue de la recherche fait ≥ 500 caractères, ou si la page complète échoue/est trop courte, on l'utilise directement plutôt que de retenter ou d'échouer ; la provenance du texte (`page` ou `search_description`) est stockée dans `enriched["text_source"]`.
- `llm.py` distingue `LLMFatal` (clé API invalide/révoquée, crédit épuisé, requête rejetée — HTTP 400/401/402/403) de `LLMError` (retentable ou erreur de contenu) : une erreur fatale fait échouer le run entier plutôt qu'une seule offre.
- Les compteurs de `discover()` distinguent `new` (offres canoniques réellement nouvelles) d'`inserted` (toutes les lignes créées, y compris les doublons détectés) — utile pour ne pas surestimer le volume net d'offres neuves.

## Carte des fichiers

| Fichier | Responsabilité |
|---|---|
| `pyproject.toml`, `.gitignore`, `.env.example` | scaffold |
| `stagefinder/config.py` | `Settings`, `load_settings()` |
| `stagefinder/db.py` | `SCHEMA`, `connect()`, `migrate()` |
| `stagefinder/models.py` | `RawJob`, `Campaign`, `Fact`, `load_campaign()`, `campaign_terms()` |
| `stagefinder/llm.py` | `LLMClient`, `extract_json()`, `LLMError`, `BudgetExceeded` |
| `stagefinder/dedup.py` | normalisation, `fingerprint()`, `extract_external_id()`, `upsert_job()` |
| `stagefinder/sources/base.py` | `Source` protocol |
| `stagefinder/sources/jobspy_src.py` | Indeed + LinkedIn |
| `stagefinder/sources/jobteaser.py` | JobTeaser HTML |
| `stagefinder/profile.py` | CV → faits, cache par hash |
| `stagefinder/enrich.py` | page complète, texte, langue, extraction JSON |
| `stagefinder/scoring.py` | pré-filtre règles + scoring LLM |
| `stagefinder/letters.py` | angle, rédaction, contrôle, docx, versions |
| `stagefinder/pipeline.py` | `run_campaign()`, table `runs` |
| `stagefinder/cli.py` | `campaign`, `run`, `letters` |
| `tests/conftest.py` | fixtures : `conn`, `fake_llm`, `settings` |
| `tests/test_*.py` | un fichier par module |

---

### Task 0 : Scaffold du projet

**Files:**
- Create: `pyproject.toml`, `.gitignore`, `.env.example`, `stagefinder/__init__.py`, `stagefinder/sources/__init__.py`, `tests/__init__.py`, `tests/conftest.py`

**Interfaces:**
- Produces: paquet importable `stagefinder`, commande `pytest` fonctionnelle, dépendances installées.

- [ ] **Step 1 : Initialiser git et corriger l'installation de jobspy**

```bash
cd "C:/Users/Mehdi/Desktop/AI search"
git init
python -m pip uninstall -y jobspy
python -m pip install python-jobspy==1.1.82 langdetect python-docx python-dotenv pytest
python -c "from jobspy import scrape_jobs; from jobspy.model import Country; print(Country.from_string('france'))"
```

Expected : `Country.FRANCE`. Si `ModuleNotFoundError`, vérifier qu'il n'existe plus de `D:\python\Lib\site-packages\jobs.py` et réinstaller.

- [ ] **Step 2 : Écrire `.gitignore`**

```gitignore
__pycache__/
*.pyc
.env
data/
*.xlsx
*.docx
*.pdf
.pytest_cache/
docs/superpowers/plans/*.bak
```

Note : `cover letter.docx`, `CP.docx`, les CV PDF et `Gradient descent.docx` sont des documents personnels, pas du code — ils restent hors du dépôt.

- [ ] **Step 3 : Écrire `.env.example`**

```dotenv
OPENROUTER_API_KEY=sk-or-v1-remplace-moi
LLM_MODEL=openai/gpt-4o-mini
MAX_LLM_CALLS_PER_RUN=300
CV_PATH=C:\Users\Mehdi\Desktop\AI search\cv_mehdi_znaidi.pdf
DB_PATH=data/stagefinder.db
LETTERS_DIR=data/letters
```

- [ ] **Step 4 : Écrire `pyproject.toml`**

```toml
[project]
name = "stagefinder"
version = "2.0.0"
requires-python = ">=3.11"
dependencies = [
  "python-jobspy==1.1.82",
  "requests",
  "beautifulsoup4",
  "PyPDF2",
  "rapidfuzz",
  "langdetect",
  "python-docx",
  "python-dotenv",
]

[project.optional-dependencies]
dev = ["pytest"]

[project.scripts]
stagefinder = "stagefinder.cli:main"

[tool.pytest.ini_options]
testpaths = ["tests"]

[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[tool.setuptools.packages.find]
include = ["stagefinder*"]
```

- [ ] **Step 5 : Créer les paquets vides et `conftest.py`**

`stagefinder/__init__.py` : vide. `stagefinder/sources/__init__.py` : vide. `tests/__init__.py` : vide.

`tests/conftest.py` :

```python
import sqlite3
from pathlib import Path

import pytest


@pytest.fixture
def tmp_db(tmp_path: Path) -> Path:
    return tmp_path / "test.db"


class FakeLLM:
    """LLM factice : file de réponses, compteur d'appels, mémorise les prompts."""

    def __init__(self, responses=None):
        self.responses = list(responses or [])
        self.calls = 0
        self.prompts = []

    def chat(self, system: str, user: str, **kw) -> str:
        self.calls += 1
        self.prompts.append((system, user))
        if not self.responses:
            raise AssertionError("FakeLLM : plus de réponses en file")
        r = self.responses.pop(0)
        return r() if callable(r) else r

    def chat_json(self, system: str, user: str, **kw):
        from stagefinder.llm import extract_json
        return extract_json(self.chat(system, user, **kw))


@pytest.fixture
def fake_llm():
    return FakeLLM()
```

- [ ] **Step 6 : Installer en editable et vérifier**

```bash
python -m pip install -e ".[dev]"
python -m pytest -q
```

Expected : `no tests ran` sans erreur d'import.

- [ ] **Step 7 : Commit**

```bash
git add pyproject.toml .gitignore .env.example stagefinder tests
git commit -m "chore: scaffold stagefinder v2 package

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 1 : Configuration et base de données

**Files:**
- Create: `stagefinder/config.py`, `stagefinder/db.py`
- Test: `tests/test_db.py`

**Interfaces:**
- Produces:
  - `config.Settings` (dataclass frozen) : `openrouter_api_key: str`, `llm_model: str`, `max_llm_calls_per_run: int`, `cv_path: Path | None`, `db_path: Path`, `letters_dir: Path`
  - `config.load_settings(env: Mapping[str, str] | None = None) -> Settings`
  - `db.connect(db_path: Path) -> sqlite3.Connection` (row_factory=Row, foreign_keys ON)
  - `db.migrate(conn) -> None` (idempotent)
  - `db.now() -> str` (ISO UTC)

- [ ] **Step 1 : Écrire le test**

`tests/test_db.py` :

```python
from stagefinder import db
from stagefinder.config import load_settings


def test_migrate_creates_tables_and_is_idempotent(tmp_db):
    conn = db.connect(tmp_db)
    db.migrate(conn)
    db.migrate(conn)  # second appel sans erreur
    tables = {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"campaigns", "campaign_terms", "jobs", "job_campaigns", "scores",
            "facts", "letters", "applications", "runs"} <= tables


def test_foreign_keys_enforced(tmp_db):
    conn = db.connect(tmp_db)
    db.migrate(conn)
    import sqlite3, pytest
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO campaign_terms(campaign_id, term, kind, origin) "
                     "VALUES (999, 'x', 'keyword', 'user')")


def test_load_settings_from_mapping(tmp_path):
    s = load_settings({
        "OPENROUTER_API_KEY": "k", "LLM_MODEL": "m", "MAX_LLM_CALLS_PER_RUN": "7",
        "DB_PATH": str(tmp_path / "x.db"), "LETTERS_DIR": str(tmp_path / "l"),
    })
    assert s.openrouter_api_key == "k"
    assert s.max_llm_calls_per_run == 7
    assert s.cv_path is None


def test_load_settings_requires_api_key():
    import pytest
    with pytest.raises(ValueError):
        load_settings({})
```

- [ ] **Step 2 : Lancer, vérifier l'échec**

Run : `python -m pytest tests/test_db.py -v`
Expected : FAIL, `ModuleNotFoundError: stagefinder.db`

- [ ] **Step 3 : Écrire `stagefinder/config.py`**

```python
from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from dotenv import dotenv_values


@dataclass(frozen=True)
class Settings:
    openrouter_api_key: str
    llm_model: str = "openai/gpt-4o-mini"
    max_llm_calls_per_run: int = 300
    cv_path: Path | None = None
    db_path: Path = Path("data/stagefinder.db")
    letters_dir: Path = Path("data/letters")


def load_settings(env: Mapping[str, str] | None = None) -> Settings:
    """Lit .env puis l'environnement (l'environnement gagne). `env` remplace les deux (tests)."""
    if env is None:
        merged = {**dotenv_values(".env"), **os.environ}
    else:
        merged = dict(env)
    key = merged.get("OPENROUTER_API_KEY")
    if not key:
        raise ValueError("OPENROUTER_API_KEY manquante (voir .env.example)")
    cv = merged.get("CV_PATH")
    return Settings(
        openrouter_api_key=key,
        llm_model=merged.get("LLM_MODEL", "openai/gpt-4o-mini"),
        max_llm_calls_per_run=int(merged.get("MAX_LLM_CALLS_PER_RUN", "300")),
        cv_path=Path(cv) if cv else None,
        db_path=Path(merged.get("DB_PATH", "data/stagefinder.db")),
        letters_dir=Path(merged.get("LETTERS_DIR", "data/letters")),
    )
```

- [ ] **Step 4 : Écrire `stagefinder/db.py`**

```python
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS campaigns (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  intent TEXT NOT NULL,
  country TEXT NOT NULL,
  job_type TEXT,
  is_remote INTEGER NOT NULL DEFAULT 0,
  hours_old INTEGER,
  letter_language TEXT NOT NULL DEFAULT 'fr',
  score_threshold INTEGER NOT NULL DEFAULT 70,
  created_at TEXT, last_run_at TEXT
);
CREATE TABLE IF NOT EXISTS campaign_terms (
  id INTEGER PRIMARY KEY,
  campaign_id INTEGER NOT NULL REFERENCES campaigns(id),
  term TEXT NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN ('keyword','must_have','exclude','location')),
  origin TEXT NOT NULL CHECK (origin IN ('ai','user')),
  enabled INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS jobs (
  id INTEGER PRIMARY KEY,
  source TEXT NOT NULL, external_id TEXT NOT NULL,
  fingerprint TEXT NOT NULL,
  company_norm TEXT NOT NULL,
  title TEXT, company TEXT, location TEXT, url TEXT,
  description TEXT,
  full_description TEXT,
  enriched TEXT,
  language TEXT,
  posted_at TEXT,
  status TEXT NOT NULL DEFAULT 'discovered',
  failure_reason TEXT,
  duplicate_of INTEGER REFERENCES jobs(id),
  first_seen_at TEXT, last_seen_at TEXT,
  seen_count INTEGER NOT NULL DEFAULT 1,
  UNIQUE(source, external_id)
);
CREATE INDEX IF NOT EXISTS idx_jobs_fingerprint ON jobs(fingerprint);
CREATE INDEX IF NOT EXISTS idx_jobs_company_norm ON jobs(company_norm);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
CREATE TABLE IF NOT EXISTS job_campaigns (
  job_id INTEGER NOT NULL REFERENCES jobs(id),
  campaign_id INTEGER NOT NULL REFERENCES campaigns(id),
  matched_keyword TEXT, matched_location TEXT,
  PRIMARY KEY (job_id, campaign_id)
);
CREATE TABLE IF NOT EXISTS scores (
  job_id INTEGER NOT NULL REFERENCES jobs(id),
  campaign_id INTEGER NOT NULL REFERENCES campaigns(id),
  score INTEGER, reasons TEXT,
  model TEXT, scored_at TEXT,
  PRIMARY KEY (job_id, campaign_id)
);
CREATE TABLE IF NOT EXISTS facts (
  id INTEGER PRIMARY KEY,
  cv_hash TEXT NOT NULL,
  kind TEXT NOT NULL,
  label TEXT, detail TEXT,
  tech TEXT,
  metric TEXT, period TEXT
);
CREATE INDEX IF NOT EXISTS idx_facts_hash ON facts(cv_hash);
CREATE TABLE IF NOT EXISTS letters (
  id INTEGER PRIMARY KEY,
  job_id INTEGER NOT NULL REFERENCES jobs(id),
  campaign_id INTEGER NOT NULL REFERENCES campaigns(id),
  version INTEGER NOT NULL DEFAULT 1,
  language TEXT NOT NULL,
  angle TEXT, body TEXT, file_path TEXT, warnings TEXT,
  model TEXT, created_at TEXT,
  UNIQUE(job_id, campaign_id, version)
);
CREATE TABLE IF NOT EXISTS applications (
  job_id INTEGER PRIMARY KEY REFERENCES jobs(id),
  letter_id INTEGER REFERENCES letters(id),
  applied_at TEXT, channel TEXT,
  outcome TEXT, notes TEXT
);
CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY,
  campaign_id INTEGER REFERENCES campaigns(id),
  status TEXT NOT NULL,
  stage TEXT,
  counters TEXT,
  llm_calls INTEGER NOT NULL DEFAULT 0,
  started_at TEXT, finished_at TEXT,
  error TEXT
);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def migrate(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()
```

- [ ] **Step 5 : Lancer, vérifier le succès**

Run : `python -m pytest tests/test_db.py -v`
Expected : 4 PASS

- [ ] **Step 6 : Commit**

```bash
git add stagefinder/config.py stagefinder/db.py tests/test_db.py
git commit -m "feat: settings loader and sqlite schema

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 2 : Client LLM

**Files:**
- Create: `stagefinder/llm.py`
- Test: `tests/test_llm.py`

**Interfaces:**
- Produces:
  - `class LLMError(Exception)`, `class BudgetExceeded(LLMError)`
  - `extract_json(text: str) -> Any` — lève `ValueError` si aucun JSON
  - `class LLMClient:`
    - `__init__(self, api_key: str, model: str, max_calls: int | None = None, post=requests.post, sleep=time.sleep)`
    - `calls: int`
    - `chat(self, system: str, user: str, temperature: float = 0.0, max_tokens: int = 800) -> str`
    - `chat_json(self, system: str, user: str, **kw) -> Any` — une reprise avec le message d'erreur injecté, puis `LLMError`

- [ ] **Step 1 : Écrire les tests**

`tests/test_llm.py` :

```python
import pytest

from stagefinder.llm import BudgetExceeded, LLMClient, LLMError, extract_json


class Resp:
    def __init__(self, status, content=None):
        self.status_code = status
        self._content = content

    def json(self):
        return {"choices": [{"message": {"content": self._content}}]}


def make_post(responses):
    seq = list(responses)
    calls = []

    def post(url, headers=None, json=None, timeout=None):
        calls.append(json)
        r = seq.pop(0)
        if isinstance(r, Exception):
            raise r
        return r

    post.calls = calls
    return post


def test_extract_json_plain_and_fenced_and_prefixed():
    assert extract_json('{"a": 1}') == {"a": 1}
    assert extract_json('```json\n[1, 2]\n```') == [1, 2]
    assert extract_json('Voici la réponse :\n{"a": [1]}\nMerci') == {"a": [1]}


def test_extract_json_raises_when_absent():
    with pytest.raises(ValueError):
        extract_json("aucun")


def test_chat_returns_content_and_counts():
    post = make_post([Resp(200, "ok")])
    c = LLMClient("k", "m", post=post, sleep=lambda s: None)
    assert c.chat("sys", "usr") == "ok"
    assert c.calls == 1
    assert post.calls[0]["model"] == "m"


def test_retry_on_429_then_success():
    post = make_post([Resp(429), Resp(500), Resp(200, "fin")])
    c = LLMClient("k", "m", post=post, sleep=lambda s: None)
    assert c.chat("s", "u") == "fin"


def test_no_retry_on_401():
    post = make_post([Resp(401)])
    c = LLMClient("k", "m", post=post, sleep=lambda s: None)
    with pytest.raises(LLMError):
        c.chat("s", "u")
    assert len(post.calls) == 1


def test_budget_exceeded():
    post = make_post([Resp(200, "a"), Resp(200, "b")])
    c = LLMClient("k", "m", max_calls=1, post=post, sleep=lambda s: None)
    c.chat("s", "u")
    with pytest.raises(BudgetExceeded):
        c.chat("s", "u")


def test_chat_json_retries_once_with_error_feedback():
    post = make_post([Resp(200, "pas du json"), Resp(200, '{"ok": true}')])
    c = LLMClient("k", "m", post=post, sleep=lambda s: None)
    assert c.chat_json("s", "u") == {"ok": True}
    assert "JSON" in post.calls[1]["messages"][-1]["content"]


def test_chat_json_fails_after_two_attempts():
    post = make_post([Resp(200, "non"), Resp(200, "toujours non")])
    c = LLMClient("k", "m", post=post, sleep=lambda s: None)
    with pytest.raises(LLMError):
        c.chat_json("s", "u")
```

- [ ] **Step 2 : Lancer, vérifier l'échec**

Run : `python -m pytest tests/test_llm.py -v`
Expected : FAIL, `ModuleNotFoundError`

- [ ] **Step 3 : Écrire `stagefinder/llm.py`**

```python
from __future__ import annotations

import json
import re
import time
from typing import Any

import requests

ENDPOINT = "https://openrouter.ai/api/v1/chat/completions"
RETRYABLE = {429, 500, 502, 503, 504}
_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)


class LLMError(Exception):
    pass


class BudgetExceeded(LLMError):
    pass


def extract_json(text: str) -> Any:
    """Premier JSON valide dans `text` : bloc ``` ou premier objet/tableau trouvé."""
    m = _FENCE.search(text)
    if m:
        text = m.group(1)
    dec = json.JSONDecoder()
    for i, ch in enumerate(text):
        if ch in "{[":
            try:
                obj, _ = dec.raw_decode(text[i:])
                return obj
            except json.JSONDecodeError:
                continue
    raise ValueError("aucun JSON dans la réponse")


class LLMClient:
    def __init__(self, api_key: str, model: str, max_calls: int | None = None,
                 post=requests.post, sleep=time.sleep):
        self.api_key = api_key
        self.model = model
        self.max_calls = max_calls
        self._post = post
        self._sleep = sleep
        self.calls = 0

    def _request(self, messages: list[dict], temperature: float, max_tokens: int) -> str:
        if self.max_calls is not None and self.calls >= self.max_calls:
            raise BudgetExceeded(f"plafond de {self.max_calls} appels LLM atteint")
        self.calls += 1
        payload = {"model": self.model, "messages": messages,
                   "temperature": temperature, "max_tokens": max_tokens}
        headers = {"Authorization": f"Bearer {self.api_key}",
                   "Content-Type": "application/json"}
        last = None
        for attempt in range(3):
            try:
                r = self._post(ENDPOINT, headers=headers, json=payload, timeout=60)
            except requests.exceptions.RequestException as e:
                last = f"réseau: {e}"
                self._sleep(2 ** attempt)
                continue
            if r.status_code == 200:
                content = r.json()["choices"][0]["message"]["content"]
                if not content:
                    raise LLMError("réponse vide")
                return content
            if r.status_code in RETRYABLE:
                last = f"HTTP {r.status_code}"
                self._sleep(2 ** attempt)
                continue
            raise LLMError(f"HTTP {r.status_code} (non retentable)")
        raise LLMError(f"échec après 3 tentatives: {last}")

    def chat(self, system: str, user: str, temperature: float = 0.0,
             max_tokens: int = 800) -> str:
        return self._request([{"role": "system", "content": system},
                              {"role": "user", "content": user}], temperature, max_tokens)

    def chat_json(self, system: str, user: str, **kw) -> Any:
        first = self.chat(system, user, **kw)
        try:
            return extract_json(first)
        except ValueError as e:
            retry_user = (f"{user}\n\nTa réponse précédente n'était pas du JSON valide "
                          f"({e}). Réponds UNIQUEMENT avec du JSON.")
            second = self.chat(system, retry_user, **kw)
            try:
                return extract_json(second)
            except ValueError:
                raise LLMError("JSON invalide après deux tentatives")
```

- [ ] **Step 4 : Lancer, vérifier le succès**

Run : `python -m pytest tests/test_llm.py -v`
Expected : 8 PASS

- [ ] **Step 5 : Commit**

```bash
git add stagefinder/llm.py tests/test_llm.py
git commit -m "feat: openrouter client with retry, budget and defensive json

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 3 : Modèles et accès aux campagnes

**Files:**
- Create: `stagefinder/models.py`
- Test: `tests/test_models.py`

**Interfaces:**
- Produces:
  - `@dataclass RawJob(source, external_id: str | None, title, company, location, url, description, posted_at: str | None)`
  - `@dataclass Campaign(id, name, intent, country, job_type, is_remote: bool, hours_old, letter_language, score_threshold)`
  - `@dataclass Fact(id, kind, label, detail, tech: list[str], metric, period)`
  - `create_campaign(conn, name, intent, country, job_type=None, is_remote=False, hours_old=None, letter_language='fr', score_threshold=70) -> int`
  - `load_campaign(conn, campaign_id) -> Campaign` — `KeyError` si absente
  - `add_terms(conn, campaign_id, kind, terms: list[str], origin='user') -> None`
  - `campaign_terms(conn, campaign_id, kind) -> list[str]` — seulement `enabled=1`
  - `row_to_fact(row) -> Fact`

- [ ] **Step 1 : Écrire les tests**

`tests/test_models.py` :

```python
import pytest

from stagefinder import db
from stagefinder.models import (add_terms, campaign_terms, create_campaign,
                                load_campaign)


@pytest.fixture
def conn(tmp_db):
    c = db.connect(tmp_db)
    db.migrate(c)
    return c


def test_create_and_load_campaign(conn):
    cid = create_campaign(conn, "DS FR", "stage data science", "FR",
                          job_type="internship", hours_old=168)
    c = load_campaign(conn, cid)
    assert c.name == "DS FR" and c.country == "FR"
    assert c.letter_language == "fr" and c.score_threshold == 70
    assert c.is_remote is False


def test_load_missing_campaign_raises(conn):
    with pytest.raises(KeyError):
        load_campaign(conn, 42)


def test_terms_filtered_by_kind_and_enabled(conn):
    cid = create_campaign(conn, "x", "y", "FR")
    add_terms(conn, cid, "keyword", ["stage data", "intern ml"])
    add_terms(conn, cid, "exclude", ["senior"])
    conn.execute("UPDATE campaign_terms SET enabled=0 WHERE term='intern ml'")
    assert campaign_terms(conn, cid, "keyword") == ["stage data"]
    assert campaign_terms(conn, cid, "exclude") == ["senior"]
    assert campaign_terms(conn, cid, "location") == []
```

- [ ] **Step 2 : Lancer, vérifier l'échec**

Run : `python -m pytest tests/test_models.py -v`
Expected : FAIL, `ModuleNotFoundError`

- [ ] **Step 3 : Écrire `stagefinder/models.py`**

```python
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass

from stagefinder.db import now


@dataclass
class RawJob:
    source: str
    external_id: str | None
    title: str
    company: str
    location: str
    url: str
    description: str = ""
    posted_at: str | None = None


@dataclass
class Campaign:
    id: int
    name: str
    intent: str
    country: str
    job_type: str | None
    is_remote: bool
    hours_old: int | None
    letter_language: str
    score_threshold: int


@dataclass
class Fact:
    id: int
    kind: str
    label: str
    detail: str
    tech: list[str]
    metric: str
    period: str


def create_campaign(conn: sqlite3.Connection, name: str, intent: str, country: str,
                    job_type: str | None = None, is_remote: bool = False,
                    hours_old: int | None = None, letter_language: str = "fr",
                    score_threshold: int = 70) -> int:
    cur = conn.execute(
        "INSERT INTO campaigns(name,intent,country,job_type,is_remote,hours_old,"
        "letter_language,score_threshold,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (name, intent, country.upper(), job_type, int(is_remote), hours_old,
         letter_language, score_threshold, now()))
    conn.commit()
    return cur.lastrowid


def load_campaign(conn: sqlite3.Connection, campaign_id: int) -> Campaign:
    r = conn.execute("SELECT * FROM campaigns WHERE id=?", (campaign_id,)).fetchone()
    if r is None:
        raise KeyError(f"campagne {campaign_id} introuvable")
    return Campaign(r["id"], r["name"], r["intent"], r["country"], r["job_type"],
                    bool(r["is_remote"]), r["hours_old"], r["letter_language"],
                    r["score_threshold"])


def add_terms(conn: sqlite3.Connection, campaign_id: int, kind: str,
              terms: list[str], origin: str = "user") -> None:
    conn.executemany(
        "INSERT INTO campaign_terms(campaign_id,term,kind,origin) VALUES (?,?,?,?)",
        [(campaign_id, t.strip(), kind, origin) for t in terms if t.strip()])
    conn.commit()


def campaign_terms(conn: sqlite3.Connection, campaign_id: int, kind: str) -> list[str]:
    rows = conn.execute(
        "SELECT term FROM campaign_terms WHERE campaign_id=? AND kind=? AND enabled=1 "
        "ORDER BY id", (campaign_id, kind)).fetchall()
    return [r["term"] for r in rows]


def row_to_fact(r: sqlite3.Row) -> Fact:
    return Fact(r["id"], r["kind"], r["label"] or "", r["detail"] or "",
                json.loads(r["tech"] or "[]"), r["metric"] or "", r["period"] or "")
```

- [ ] **Step 4 : Lancer, vérifier le succès**

Run : `python -m pytest tests/test_models.py -v`
Expected : 3 PASS

- [ ] **Step 5 : Commit**

```bash
git add stagefinder/models.py tests/test_models.py
git commit -m "feat: campaign and fact models

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 4 : Déduplication

**Files:**
- Create: `stagefinder/dedup.py`
- Test: `tests/test_dedup.py`

**Interfaces:**
- Consumes: `models.RawJob`, `db.now`
- Produces:
  - `normalize_title(t: str) -> str` — tokens canonisés et triés
  - `normalize_company(c: str) -> str`
  - `fingerprint(company: str, title: str) -> str` — sha1 hex
  - `extract_external_id(source: str, url: str, fallback: str | None) -> str | None`
  - `@dataclass UpsertResult(job_id: int, created: bool, duplicate_of: int | None)`
  - `upsert_job(conn, raw: RawJob, campaign_id: int, matched_keyword: str, matched_location: str) -> UpsertResult`

- [ ] **Step 1 : Écrire les tests**

`tests/test_dedup.py` :

```python
import pytest

from stagefinder import db
from stagefinder.dedup import (extract_external_id, fingerprint, normalize_company,
                               normalize_title, upsert_job)
from stagefinder.models import RawJob, create_campaign


@pytest.fixture
def conn(tmp_db):
    c = db.connect(tmp_db)
    db.migrate(c)
    return c


@pytest.fixture
def cid(conn):
    return create_campaign(conn, "c", "i", "FR")


@pytest.mark.parametrize("a,b", [
    ("Stage Data Scientist H/F - 6 mois", "Data Scientist Intern (M/F)"),
    ("Stagiaire Data Scientist (F/H) 2026", "Stage – Data Scientist"),
    ("Praktikum Machine Learning (m/w/d)", "Machine Learning Internship"),
])
def test_normalize_title_converges(a, b):
    assert normalize_title(a) == normalize_title(b)


def test_normalize_title_keeps_distinguishing_words():
    assert normalize_title("Stage Data Scientist") != normalize_title("Stage Data Engineer")
    assert normalize_title("Stage Data Scientist") != normalize_title("CDI Data Scientist")


@pytest.mark.parametrize("a,b", [
    ("Criteo SA", "Criteo"), ("BUTAGAZ SAS", "Butagaz"),
    ("Siemens GmbH", "siemens"), ("Doctolib via Indeed", "Doctolib"),
])
def test_normalize_company(a, b):
    assert normalize_company(a) == normalize_company(b)


def test_fingerprint_is_stable_and_ignores_location():
    assert fingerprint("Criteo SA", "Stage Data Scientist H/F") == \
        fingerprint("Criteo", "Data Scientist Intern (M/F)")


@pytest.mark.parametrize("source,url,fb,expected", [
    ("indeed", "https://fr.indeed.com/viewjob?jk=a1b2c3d4e5f6&from=serp", None, "a1b2c3d4e5f6"),
    ("indeed", "https://fr.indeed.com/rc/clk?jk=ffff0000&tk=x", None, "ffff0000"),
    ("linkedin", "https://www.linkedin.com/jobs/view/3912345678/?ref=x", None, "3912345678"),
    ("linkedin", "https://fr.linkedin.com/jobs/view/data-scientist-at-x-3912345678", None, "3912345678"),
    ("indeed", "https://weird/url", "in-zzz", "zzz"),
    ("jobteaser", "https://www.jobteaser.com/fr/job-offers/12345-stage-data", None, "12345-stage-data"),
    ("indeed", "https://weird/url", None, None),
])
def test_extract_external_id(source, url, fb, expected):
    assert extract_external_id(source, url, fb) == expected


def _raw(source="indeed", ext="k1", title="Stage Data Scientist H/F",
         company="Criteo SA", url="https://fr.indeed.com/viewjob?jk=k1", loc="Paris (75)"):
    return RawJob(source, ext, title, company, loc, url, "desc")


def test_layer1_same_source_id_increments_seen(conn, cid):
    r1 = upsert_job(conn, _raw(), cid, "kw", "Paris")
    r2 = upsert_job(conn, _raw(), cid, "kw2", "Paris")
    assert r1.created and not r2.created and r1.job_id == r2.job_id
    row = conn.execute("SELECT seen_count, status FROM jobs WHERE id=?", (r1.job_id,)).fetchone()
    assert row["seen_count"] == 2 and row["status"] == "discovered"
    assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1


def test_layer2_cross_platform_same_offer_is_duplicate(conn, cid):
    a = upsert_job(conn, _raw(), cid, "kw", "Paris")
    b = upsert_job(conn, _raw(source="linkedin", ext="99", title="Data Scientist Intern (M/F)",
                        company="Criteo", url="https://www.linkedin.com/jobs/view/99",
                        loc="Paris, Île-de-France, France"), cid, "kw", "Paris")
    assert b.created and b.duplicate_of == a.job_id
    st = conn.execute("SELECT status FROM jobs WHERE id=?", (b.job_id,)).fetchone()["status"]
    assert st == "duplicate"


def test_layer3_fuzzy_same_company_city_suffix(conn, cid):
    a = upsert_job(conn, _raw(), cid, "kw", "Paris")
    b = upsert_job(conn, _raw(source="linkedin", ext="77",
                        title="Stage Data Scientist H/F - Paris",
                        url="https://www.linkedin.com/jobs/view/77"), cid, "kw", "Paris")
    assert b.duplicate_of == a.job_id


def test_distinct_roles_same_company_not_merged(conn, cid):
    a = upsert_job(conn, _raw(), cid, "kw", "Paris")
    b = upsert_job(conn, _raw(ext="k2", title="Stage Data Engineer H/F",
                        url="https://fr.indeed.com/viewjob?jk=k2"), cid, "kw", "Paris")
    assert b.duplicate_of is None and b.job_id != a.job_id


def test_job_linked_to_campaign(conn, cid):
    r = upsert_job(conn, _raw(), cid, "stage data", "Paris")
    row = conn.execute("SELECT * FROM job_campaigns WHERE job_id=?", (r.job_id,)).fetchone()
    assert row["campaign_id"] == cid and row["matched_keyword"] == "stage data"


def test_no_external_id_falls_back_to_fingerprint(conn, cid):
    r = upsert_job(conn, _raw(ext=None, url="https://weird"), cid, "k", "l")
    ext = conn.execute("SELECT external_id FROM jobs WHERE id=?", (r.job_id,)).fetchone()[0]
    assert ext.startswith("fp:")
```

- [ ] **Step 2 : Lancer, vérifier l'échec**

Run : `python -m pytest tests/test_dedup.py -v`
Expected : FAIL, `ModuleNotFoundError`

- [ ] **Step 3 : Écrire `stagefinder/dedup.py`**

```python
from __future__ import annotations

import hashlib
import re
import sqlite3
from dataclasses import dataclass

from rapidfuzz import fuzz

from stagefinder.db import now
from stagefinder.models import RawJob

FUZZY_THRESHOLD = 90

# Bruit RH par langue : (h/f), F/H, (m/w/d), M/F/D, (H/M)…
_GENDER = re.compile(r"\(?\b[hfmwdx]\s*/\s*[hfmwdx](?:\s*/\s*[hfmwdx])?\b\)?", re.I)
_DURATION = re.compile(r"\b\d+\s*(?:mois|months?|monate|meses)\b", re.I)
_YEAR = re.compile(r"\b20\d\d\b")
_PUNCT = re.compile(r"[^\w\s]", re.UNICODE)
_WS = re.compile(r"\s+")

# Synonymes canonisés vers un seul token
_SYNONYMS = {
    "stagiaire": "stage", "internship": "stage", "intern": "stage",
    "praktikum": "stage", "praktikant": "stage", "praktikantin": "stage",
    "werkstudent": "stage", "werkstudentin": "stage", "pasantia": "stage",
    "tirocinio": "stage", "estagio": "stage",
}
_LEGAL = re.compile(
    r"\b(sas|sa|sarl|sasu|eurl|gmbh|ag|ltd|limited|inc|llc|plc|bv|nv|srl|spa|"
    r"group|groupe|france|via indeed)\b", re.I)


def normalize_title(t: str) -> str:
    t = (t or "").lower()
    t = _GENDER.sub(" ", t)
    t = _DURATION.sub(" ", t)
    t = _YEAR.sub(" ", t)
    t = _PUNCT.sub(" ", t)
    toks = [_SYNONYMS.get(w, w) for w in _WS.split(t) if w]
    return " ".join(sorted(set(toks)))


def normalize_company(c: str) -> str:
    c = (c or "").lower()
    c = _LEGAL.sub(" ", c)
    c = _PUNCT.sub(" ", c)
    return _WS.sub(" ", c).strip()


def fingerprint(company: str, title: str) -> str:
    key = f"{normalize_company(company)}|{normalize_title(title)}"
    return hashlib.sha1(key.encode("utf-8")).hexdigest()


_ID_PATTERNS = {
    "indeed": [re.compile(r"[?&]v?jk=([0-9a-f]+)", re.I)],
    "linkedin": [re.compile(r"/jobs/view/(?:[^/?#]*?-)?(\d{6,})")],
    "jobteaser": [re.compile(r"/job-offers/([^/?#]+)")],
}


def extract_external_id(source: str, url: str, fallback: str | None) -> str | None:
    for pat in _ID_PATTERNS.get(source, []):
        m = pat.search(url or "")
        if m:
            return m.group(1)
    if fallback:
        return fallback.split("-", 1)[1] if re.match(r"^(in|li|gd|zr)-", fallback) else fallback
    return None


@dataclass
class UpsertResult:
    job_id: int
    created: bool
    duplicate_of: int | None


def _find_canonical(conn: sqlite3.Connection, fp: str, company_norm: str,
                    title_norm: str) -> int | None:
    row = conn.execute(
        "SELECT id FROM jobs WHERE fingerprint=? AND duplicate_of IS NULL ORDER BY id LIMIT 1",
        (fp,)).fetchone()
    if row:
        return row["id"]
    for cand in conn.execute(
            "SELECT id, title FROM jobs WHERE company_norm=? AND duplicate_of IS NULL",
            (company_norm,)):
        if fuzz.token_set_ratio(title_norm, normalize_title(cand["title"])) >= FUZZY_THRESHOLD:
            return cand["id"]
    return None


def _link(conn: sqlite3.Connection, job_id: int, campaign_id: int, kw: str, loc: str) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO job_campaigns(job_id,campaign_id,matched_keyword,matched_location) "
        "VALUES (?,?,?,?)", (job_id, campaign_id, kw, loc))


def upsert_job(conn: sqlite3.Connection, raw: RawJob, campaign_id: int,
               matched_keyword: str, matched_location: str) -> UpsertResult:
    fp = fingerprint(raw.company, raw.title)
    company_norm = normalize_company(raw.company)
    title_norm = normalize_title(raw.title)
    ext = extract_external_id(raw.source, raw.url, raw.external_id) or f"fp:{fp}"
    ts = now()

    existing = conn.execute(
        "SELECT id, duplicate_of FROM jobs WHERE source=? AND external_id=?",
        (raw.source, ext)).fetchone()
    if existing:
        conn.execute("UPDATE jobs SET last_seen_at=?, seen_count=seen_count+1 WHERE id=?",
                     (ts, existing["id"]))
        _link(conn, existing["id"], campaign_id, matched_keyword, matched_location)
        if existing["duplicate_of"]:
            _link(conn, existing["duplicate_of"], campaign_id, matched_keyword, matched_location)
        conn.commit()
        return UpsertResult(existing["id"], False, existing["duplicate_of"])

    canonical = _find_canonical(conn, fp, company_norm, title_norm)
    status = "duplicate" if canonical else "discovered"
    cur = conn.execute(
        "INSERT INTO jobs(source,external_id,fingerprint,company_norm,title,company,location,"
        "url,description,posted_at,status,duplicate_of,first_seen_at,last_seen_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (raw.source, ext, fp, company_norm, raw.title, raw.company, raw.location, raw.url,
         raw.description, raw.posted_at, status, canonical, ts, ts))
    job_id = cur.lastrowid
    _link(conn, job_id, campaign_id, matched_keyword, matched_location)
    if canonical:
        conn.execute("UPDATE jobs SET last_seen_at=?, seen_count=seen_count+1 WHERE id=?",
                     (ts, canonical))
        _link(conn, canonical, campaign_id, matched_keyword, matched_location)
    conn.commit()
    return UpsertResult(job_id, True, canonical)
```

- [ ] **Step 4 : Lancer, vérifier le succès**

Run : `python -m pytest tests/test_dedup.py -v`
Expected : tous PASS. Si `test_layer3_fuzzy_same_company_city_suffix` échoue, afficher `fuzz.token_set_ratio(normalize_title(a), normalize_title(b))` et ajuster `FUZZY_THRESHOLD` **vers le bas uniquement si** `test_distinct_roles_same_company_not_merged` reste vert.

- [ ] **Step 5 : Commit**

```bash
git add stagefinder/dedup.py tests/test_dedup.py
git commit -m "feat: four-layer job deduplication at write time

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 5 : Sources (JobSpy + JobTeaser)

**Files:**
- Create: `stagefinder/sources/base.py`, `stagefinder/sources/jobspy_src.py`, `stagefinder/sources/jobteaser.py`
- Test: `tests/test_sources.py`, `tests/fixtures/jobteaser.html`

**Interfaces:**
- Consumes: `models.RawJob`
- Produces:
  - `base.Source` (Protocol) : `name: str`, `supports(country: str) -> bool`, `search(keyword, location, country, job_type, hours_old, is_remote, limit) -> list[RawJob]`
  - `jobspy_src.JobSpySource(sites=("indeed","linkedin"), scrape=jobspy.scrape_jobs)`
  - `jobspy_src.INDEED_COUNTRIES: dict[str, str]` — ISO → nom jobspy
  - `jobspy_src.rows_to_rawjobs(df) -> list[RawJob]`
  - `jobteaser.JobTeaserSource(get=requests.get)` ; `jobteaser.parse_jobteaser(html: str, location: str) -> list[RawJob]`

- [ ] **Step 1 : Créer la fixture HTML**

`tests/fixtures/jobteaser.html` :

```html
<html><body>
<div class="job-offer-card">
  <a href="/fr/job-offers/1001-stage-data-scientist">
    <h3 class="job-offer-card__title">Stage Data Scientist H/F</h3>
  </a>
  <span class="job-offer-card__company">Criteo</span>
</div>
<div class="job-offer-card">
  <a href="/fr/job-offers/1002-stage-ml">
    <h3 class="job-offer-card__title">Stage Machine Learning</h3>
  </a>
  <span class="job-offer-card__company">Doctolib</span>
</div>
<div class="job-offer-card"><span class="job-offer-card__company">Sans titre</span></div>
</body></html>
```

- [ ] **Step 2 : Écrire les tests**

`tests/test_sources.py` :

```python
from pathlib import Path

import pandas as pd

from stagefinder.sources.jobspy_src import INDEED_COUNTRIES, JobSpySource, rows_to_rawjobs
from stagefinder.sources.jobteaser import JobTeaserSource, parse_jobteaser

FIX = Path(__file__).parent / "fixtures"


def test_rows_to_rawjobs_maps_columns():
    df = pd.DataFrame([{
        "id": "in-abc", "site": "indeed", "job_url": "https://fr.indeed.com/viewjob?jk=abc",
        "title": "Stage DS", "company": "Criteo", "location": "Paris, FR",
        "date_posted": pd.Timestamp("2026-09-18"), "description": "desc",
    }, {
        "id": "li-123", "site": "linkedin", "job_url": "https://www.linkedin.com/jobs/view/123",
        "title": "Intern", "company": None, "location": None, "date_posted": None,
        "description": None,
    }])
    jobs = rows_to_rawjobs(df)
    assert jobs[0].source == "indeed" and jobs[0].external_id == "in-abc"
    assert jobs[0].posted_at == "2026-09-18"
    assert jobs[1].company == "" and jobs[1].description == "" and jobs[1].posted_at is None


def test_jobspy_supports_uses_indeed_map():
    s = JobSpySource(scrape=lambda **kw: pd.DataFrame())
    assert s.supports("FR") and s.supports("de")
    assert s.supports("TN")  # LinkedIn couvre tout : la source reste utilisable
    assert "FR" in INDEED_COUNTRIES and INDEED_COUNTRIES["FR"] == "france"


def test_jobspy_search_drops_indeed_for_unsupported_country():
    seen = {}

    def scrape(**kw):
        seen.update(kw)
        return pd.DataFrame()

    JobSpySource(scrape=scrape).search("ml", "Tunis", "TN", "internship", 168, False, 5)
    assert seen["site_name"] == ["linkedin"]
    JobSpySource(scrape=scrape).search("ml", "Paris", "FR", "internship", 168, False, 5)
    assert seen["site_name"] == ["indeed", "linkedin"]
    assert seen["country_indeed"] == "france" and seen["job_type"] == "internship"
    assert seen["hours_old"] == 168 and seen["results_wanted"] == 5


def test_jobspy_search_swallows_scraper_error_as_empty(caplog):
    def scrape(**kw):
        raise RuntimeError("boom")
    assert JobSpySource(scrape=scrape).search("a", "b", "FR", None, None, False, 5) == []


def test_parse_jobteaser_fixture():
    html = (FIX / "jobteaser.html").read_text(encoding="utf-8")
    jobs = parse_jobteaser(html, "Paris")
    assert len(jobs) == 2
    assert jobs[0].source == "jobteaser"
    assert jobs[0].url == "https://www.jobteaser.com/fr/job-offers/1001-stage-data-scientist"
    assert jobs[0].external_id == "1001-stage-data-scientist"
    assert jobs[1].company == "Doctolib" and jobs[1].location == "Paris"


def test_jobteaser_supports_europe_only():
    s = JobTeaserSource(get=lambda *a, **k: None)
    assert s.supports("FR") and s.supports("DE") and not s.supports("US")
```

- [ ] **Step 3 : Lancer, vérifier l'échec**

Run : `python -m pytest tests/test_sources.py -v`
Expected : FAIL, `ModuleNotFoundError`

- [ ] **Step 4 : Écrire `stagefinder/sources/base.py`**

```python
from __future__ import annotations

from typing import Protocol

from stagefinder.models import RawJob


class Source(Protocol):
    name: str

    def supports(self, country: str) -> bool: ...

    def search(self, keyword: str, location: str, country: str, job_type: str | None,
               hours_old: int | None, is_remote: bool, limit: int) -> list[RawJob]: ...
```

- [ ] **Step 5 : Écrire `stagefinder/sources/jobspy_src.py`**

```python
from __future__ import annotations

import logging

import pandas as pd

from stagefinder.models import RawJob

log = logging.getLogger(__name__)

# ISO 3166-1 alpha-2 → nom accepté par jobspy.model.Country.from_string
INDEED_COUNTRIES = {
    "AR": "argentina", "AU": "australia", "AT": "austria", "BH": "bahrain", "BD": "bangladesh",
    "BE": "belgium", "BG": "bulgaria", "BR": "brazil", "CA": "canada", "CL": "chile",
    "CN": "china", "CO": "colombia", "CR": "costa rica", "HR": "croatia", "CY": "cyprus",
    "CZ": "czech republic", "DK": "denmark", "EC": "ecuador", "EG": "egypt", "EE": "estonia",
    "FI": "finland", "FR": "france", "DE": "germany", "GR": "greece", "HK": "hong kong",
    "HU": "hungary", "IN": "india", "ID": "indonesia", "IE": "ireland", "IL": "israel",
    "IT": "italy", "JP": "japan", "KW": "kuwait", "LV": "latvia", "LT": "lithuania",
    "LU": "luxembourg", "MY": "malaysia", "MT": "malta", "MX": "mexico", "MA": "morocco",
    "NL": "netherlands", "NZ": "new zealand", "NG": "nigeria", "NO": "norway", "OM": "oman",
    "PK": "pakistan", "PA": "panama", "PE": "peru", "PH": "philippines", "PL": "poland",
    "PT": "portugal", "QA": "qatar", "RO": "romania", "SA": "saudi arabia", "SG": "singapore",
    "SK": "slovakia", "SI": "slovenia", "ZA": "south africa", "KR": "south korea",
    "ES": "spain", "SE": "sweden", "CH": "switzerland", "TW": "taiwan", "TH": "thailand",
    "TR": "turkey", "UA": "ukraine", "AE": "united arab emirates", "GB": "uk", "US": "usa",
    "UY": "uruguay", "VE": "venezuela", "VN": "vietnam",
}


def _default_scrape(**kw):
    from jobspy import scrape_jobs
    return scrape_jobs(**kw)


def rows_to_rawjobs(df: pd.DataFrame) -> list[RawJob]:
    def s(v) -> str:
        return "" if v is None or (isinstance(v, float) and pd.isna(v)) else str(v)

    out = []
    for _, r in df.iterrows():
        dp = r.get("date_posted")
        posted = None if dp is None or pd.isna(dp) else str(pd.Timestamp(dp).date())
        out.append(RawJob(source=s(r.get("site")), external_id=s(r.get("id")) or None,
                          title=s(r.get("title")), company=s(r.get("company")),
                          location=s(r.get("location")), url=s(r.get("job_url")),
                          description=s(r.get("description")), posted_at=posted))
    return out


class JobSpySource:
    name = "jobspy"

    def __init__(self, sites=("indeed", "linkedin"), scrape=_default_scrape):
        self.sites = list(sites)
        self._scrape = scrape

    def supports(self, country: str) -> bool:
        return True  # LinkedIn accepte toute localisation ; Indeed est filtré dans search()

    def search(self, keyword, location, country, job_type, hours_old, is_remote, limit):
        country = country.upper()
        sites = [x for x in self.sites if x != "indeed" or country in INDEED_COUNTRIES]
        if not sites:
            return []
        kw = dict(site_name=sites, search_term=keyword, location=location,
                  results_wanted=limit, hours_old=hours_old, is_remote=is_remote,
                  job_type=job_type, country_indeed=INDEED_COUNTRIES.get(country, "usa"),
                  linkedin_fetch_description=True, verbose=0)
        try:
            df = self._scrape(**kw)
        except Exception as e:  # le scraper tiers lève des exceptions variées
            log.warning("jobspy %r @ %r: %s", keyword, location, e)
            return []
        if df is None or df.empty:
            return []
        return rows_to_rawjobs(df)
```

- [ ] **Step 6 : Écrire `stagefinder/sources/jobteaser.py`**

```python
from __future__ import annotations

import logging
from urllib.parse import quote_plus

import requests
from bs4 import BeautifulSoup

from stagefinder.models import RawJob

log = logging.getLogger(__name__)
BASE = "https://www.jobteaser.com"
EU = {"FR", "DE", "ES", "IT", "BE", "NL", "LU", "CH", "AT", "PT", "PL", "SE", "DK", "FI",
      "NO", "IE", "GB", "CZ"}


def parse_jobteaser(html: str, location: str) -> list[RawJob]:
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for card in soup.select(".job-offer-card"):
        title = card.select_one(".job-offer-card__title")
        link = card.select_one("a[href]")
        if not title or not link:
            continue
        href = link["href"]
        url = href if href.startswith("http") else BASE + href
        company = card.select_one(".job-offer-card__company")
        out.append(RawJob(source="jobteaser", external_id=None,
                          title=title.get_text(strip=True),
                          company=company.get_text(strip=True) if company else "",
                          location=location, url=url))
    return out


class JobTeaserSource:
    name = "jobteaser"

    def __init__(self, get=requests.get):
        self._get = get

    def supports(self, country: str) -> bool:
        return country.upper() in EU

    def search(self, keyword, location, country, job_type, hours_old, is_remote, limit):
        url = f"{BASE}/fr/job-offers?query={quote_plus(keyword)}&location={quote_plus(location)}"
        try:
            r = self._get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
        except requests.exceptions.RequestException as e:
            log.warning("jobteaser %r: %s", keyword, e)
            return []
        if r is None or r.status_code != 200:
            log.warning("jobteaser status %s", getattr(r, "status_code", None))
            return []
        return parse_jobteaser(r.text, location)[:limit]
```

- [ ] **Step 7 : Lancer, vérifier le succès**

Run : `python -m pytest tests/test_sources.py -v`
Expected : 6 PASS

- [ ] **Step 8 : Vérifier la carte des pays contre l'énumération réelle**

```bash
python -c "
from jobspy.model import Country
from stagefinder.sources.jobspy_src import INDEED_COUNTRIES
bad=[(k,v) for k,v in INDEED_COUNTRIES.items() if not any(v in c.value[0].split(',') for c in Country)]
print('invalides:', bad)"
```

Expected : `invalides: []`. Sinon corriger les noms dans `INDEED_COUNTRIES`.

- [ ] **Step 9 : Commit**

```bash
git add stagefinder/sources tests/test_sources.py tests/fixtures/jobteaser.html
git commit -m "feat: jobspy and jobteaser sources behind a common protocol

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 6 : Profil — CV vers dossier de faits

**Files:**
- Create: `stagefinder/profile.py`
- Test: `tests/test_profile.py`

**Interfaces:**
- Consumes: `LLMClient.chat_json`, `models.Fact`, `models.row_to_fact`
- Produces:
  - `cv_hash(path: Path) -> str`
  - `extract_pdf_text(path: Path) -> str`
  - `load_or_extract_facts(conn, llm, cv_path: Path) -> list[Fact]` — n'appelle le LLM que si le hash est inconnu
  - `facts_by_ids(facts: list[Fact], ids: list[int]) -> list[Fact]`
  - `format_facts(facts: list[Fact]) -> str` — bloc texte numéroté pour les prompts

- [ ] **Step 1 : Écrire les tests**

`tests/test_profile.py` :

```python
import json

import pytest

from stagefinder import db
from stagefinder import profile as P


@pytest.fixture
def conn(tmp_db):
    c = db.connect(tmp_db)
    db.migrate(c)
    return c


FACTS_JSON = json.dumps({"facts": [
    {"kind": "project", "label": "Agent de veille scientifique",
     "detail": "Pipeline de collecte et synthèse d'articles", "tech": ["LangChain", "FAISS"],
     "metric": "10 000 articles traités", "period": "2025"},
    {"kind": "skill", "label": "Python", "detail": "", "tech": ["Python"], "metric": "", "period": ""},
]})


def test_extract_and_cache(conn, fake_llm, tmp_path, monkeypatch):
    cv = tmp_path / "cv.pdf"
    cv.write_bytes(b"%PDF-fake")
    monkeypatch.setattr(P, "extract_pdf_text", lambda p: "CV texte")
    fake_llm.responses = [FACTS_JSON]

    facts = P.load_or_extract_facts(conn, fake_llm, cv)
    assert len(facts) == 2 and facts[0].tech == ["LangChain", "FAISS"]
    assert fake_llm.calls == 1

    again = P.load_or_extract_facts(conn, fake_llm, cv)
    assert [f.id for f in again] == [f.id for f in facts]
    assert fake_llm.calls == 1  # cache : aucun second appel


def test_changed_cv_reextracts(conn, fake_llm, tmp_path, monkeypatch):
    cv = tmp_path / "cv.pdf"
    cv.write_bytes(b"v1")
    monkeypatch.setattr(P, "extract_pdf_text", lambda p: "x")
    fake_llm.responses = [FACTS_JSON, FACTS_JSON]
    P.load_or_extract_facts(conn, fake_llm, cv)
    cv.write_bytes(b"v2")
    P.load_or_extract_facts(conn, fake_llm, cv)
    assert fake_llm.calls == 2


def test_format_and_select(conn, fake_llm, tmp_path, monkeypatch):
    cv = tmp_path / "cv.pdf"; cv.write_bytes(b"z")
    monkeypatch.setattr(P, "extract_pdf_text", lambda p: "x")
    fake_llm.responses = [FACTS_JSON]
    facts = P.load_or_extract_facts(conn, fake_llm, cv)
    txt = P.format_facts(facts)
    assert f"[{facts[0].id}]" in txt and "10 000 articles" in txt
    assert P.facts_by_ids(facts, [facts[1].id]) == [facts[1]]


def test_empty_pdf_text_raises(conn, fake_llm, tmp_path, monkeypatch):
    cv = tmp_path / "cv.pdf"; cv.write_bytes(b"z")
    monkeypatch.setattr(P, "extract_pdf_text", lambda p: "   ")
    with pytest.raises(ValueError):
        P.load_or_extract_facts(conn, fake_llm, cv)
```

- [ ] **Step 2 : Lancer, vérifier l'échec**

Run : `python -m pytest tests/test_profile.py -v`
Expected : FAIL, `ModuleNotFoundError`

- [ ] **Step 3 : Écrire `stagefinder/profile.py`**

```python
from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import PyPDF2

from stagefinder.models import Fact, row_to_fact

SYSTEM = """Tu extrais d'un CV des FAITS atomiques et vérifiables, pour rédiger des lettres de motivation.
Chaque fait : {"kind": "experience|project|skill|education|achievement", "label": "titre court",
"detail": "ce qui a été fait, concrètement, 1-2 phrases", "tech": ["outils cités"], "metric": "chiffre ou résultat s'il existe, sinon \"\"", "period": "dates ou \"\""}.
Règles : n'invente rien, ne reformule pas en plus flatteur, garde les chiffres exacts, une entrée par projet/expérience/diplôme, et une entrée "skill" par groupe de compétences.
Réponds UNIQUEMENT en JSON : {"facts": [...]}"""


def cv_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def extract_pdf_text(path: Path) -> str:
    with path.open("rb") as f:
        reader = PyPDF2.PdfReader(f)
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
    if not text.strip():
        raise ValueError(f"aucun texte extrait de {cv_path} (PDF scanné ?)")
    data = llm.chat_json(SYSTEM, f"CV :\n\n{text[:6000]}", max_tokens=2000)
    facts = data.get("facts") if isinstance(data, dict) else None
    if not facts:
        raise ValueError("le LLM n'a renvoyé aucun fait")
    conn.executemany(
        "INSERT INTO facts(cv_hash,kind,label,detail,tech,metric,period) VALUES (?,?,?,?,?,?,?)",
        [(h, f.get("kind", "skill"), f.get("label", ""), f.get("detail", ""),
          json.dumps(f.get("tech") or [], ensure_ascii=False), f.get("metric", "") or "",
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
```

- [ ] **Step 4 : Lancer, vérifier le succès**

Run : `python -m pytest tests/test_profile.py -v`
Expected : 4 PASS

- [ ] **Step 5 : Commit**

```bash
git add stagefinder/profile.py tests/test_profile.py
git commit -m "feat: extract cv facts once, cached by pdf hash

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 7 : Enrichissement

**Files:**
- Create: `stagefinder/enrich.py`
- Test: `tests/test_enrich.py`

**Interfaces:**
- Consumes: `LLMClient.chat_json`, `db.now`
- Produces:
  - `fetch_page(url: str, timeout: int = 15) -> str` — lève sur erreur HTTP/réseau
  - `html_to_text(html: str) -> str` — sans script/style/nav, entités décodées
  - `detect_language(text: str) -> str` — code 2 lettres, `"und"` si échec
  - `enrich_job(conn, llm, job_id: int, fetch=fetch_page) -> bool` — True si `enriched`, False si `failed_enrich`
  - `enrich_pending(conn, llm, fetch=fetch_page, workers: int = 4) -> dict` — `{"enriched": n, "failed": m}`
  - Format de `jobs.enriched` (JSON) : `{"missions": str, "requirements": [str], "tech_stack": [str], "team_context": str}`

- [ ] **Step 1 : Écrire les tests**

`tests/test_enrich.py` :

```python
import json

import pytest

from stagefinder import db, enrich as E
from stagefinder.dedup import upsert_job
from stagefinder.models import RawJob, create_campaign

PAGE = """<html><head><title>x</title><script>var a=1</script></head><body>
<nav>menu</nav><h1>Stage Data &amp; IA</h1>
<p>Missions : nettoyage de données, gouvernance. Stack : Python, SQL, Talend.</p>
<p>Vous rejoindrez une équipe data qui structure les référentiels du groupe, définit les règles de qualité et automatise les contrôles sur les flux entrants. Le stage dure six mois et débute en février.</p>
<style>.a{}</style></body></html>"""

ENRICHED = json.dumps({"missions": "nettoyage et gouvernance", "requirements": ["Python", "SQL"],
                       "tech_stack": ["Python", "SQL", "Talend"], "team_context": "équipe data"})


@pytest.fixture
def conn(tmp_db):
    c = db.connect(tmp_db); db.migrate(c); return c


def _seed(conn, n=1, status="discovered"):
    cid = create_campaign(conn, "c", "i", "FR")
    ids = []
    for i in range(n):
        r = upsert_job(conn, RawJob("indeed", f"k{i}", f"Stage {i}", f"Co{i}", "Paris",
                                    f"https://fr.indeed.com/viewjob?jk=k{i}", "d"), cid, "k", "l")
        ids.append(r.job_id)
    if status != "discovered":
        conn.execute("UPDATE jobs SET status=?", (status,)); conn.commit()
    return cid, ids


def test_html_to_text_strips_and_unescapes():
    t = E.html_to_text(PAGE)
    assert "Stage Data & IA" in t and "var a" not in t and "menu" not in t and ".a{}" not in t


def test_detect_language():
    assert E.detect_language("Nous recherchons un stagiaire en science des données pour six mois.") == "fr"
    assert E.detect_language("We are looking for a data science intern for six months.") == "en"
    assert E.detect_language("") == "und"


def test_enrich_job_success(conn, fake_llm):
    _, (jid,) = _seed(conn)
    fake_llm.responses = [ENRICHED]
    assert E.enrich_job(conn, fake_llm, jid, fetch=lambda u, timeout=15: PAGE)
    row = conn.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone()
    assert row["status"] == "enriched" and row["language"] == "fr"
    assert "Stage Data & IA" in row["full_description"]
    assert json.loads(row["enriched"])["tech_stack"] == ["Python", "SQL", "Talend"]


def test_enrich_job_fetch_failure_marks_failed(conn, fake_llm):
    _, (jid,) = _seed(conn)

    def boom(u, timeout=15):
        raise RuntimeError("404")
    assert not E.enrich_job(conn, fake_llm, jid, fetch=boom)
    row = conn.execute("SELECT status, failure_reason FROM jobs WHERE id=?", (jid,)).fetchone()
    assert row["status"] == "failed_enrich" and "404" in row["failure_reason"]
    assert fake_llm.calls == 0


def test_enrich_pending_only_touches_discovered(conn, fake_llm):
    _, ids = _seed(conn, 3)
    conn.execute("UPDATE jobs SET status='enriched' WHERE id=?", (ids[0],)); conn.commit()
    fake_llm.responses = [ENRICHED, ENRICHED]
    c = E.enrich_pending(conn, fake_llm, fetch=lambda u, timeout=15: PAGE, workers=2)
    assert c == {"enriched": 2, "failed": 0} and fake_llm.calls == 2


def test_enrich_pending_skips_duplicates(conn, fake_llm):
    _, (jid,) = _seed(conn)
    conn.execute("UPDATE jobs SET status='duplicate'"); conn.commit()
    assert E.enrich_pending(conn, fake_llm, fetch=lambda u, timeout=15: PAGE) == {"enriched": 0, "failed": 0}
```

- [ ] **Step 2 : Lancer, vérifier l'échec**

Run : `python -m pytest tests/test_enrich.py -v`
Expected : FAIL, `ModuleNotFoundError`

- [ ] **Step 3 : Écrire `stagefinder/enrich.py`**

```python
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
```

- [ ] **Step 4 : Lancer, vérifier le succès**

Run : `python -m pytest tests/test_enrich.py -v`
Expected : 6 PASS

- [ ] **Step 5 : Commit**

```bash
git add stagefinder/enrich.py tests/test_enrich.py
git commit -m "feat: enrich jobs with full page text, language and structured extraction

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 8 : Scoring

**Files:**
- Create: `stagefinder/scoring.py`
- Test: `tests/test_scoring.py`

**Interfaces:**
- Consumes: `models.Campaign`, `models.campaign_terms`, `profile.format_facts`, `LLMClient.chat_json`
- Produces:
  - `rule_prefilter(text: str, must_have: list[str], exclude: list[str]) -> bool`
  - `score_pending(conn, llm, campaign: Campaign, facts: list[Fact], batch_size: int = 5) -> dict` — `{"scored": n, "skipped": m, "failed": k}` ; écrit `scores`, passe le statut à `scored` / `skipped` / `failed_score`
  - Format de `scores.reasons` : JSON `list[str]`

- [ ] **Step 1 : Écrire les tests**

`tests/test_scoring.py` :

```python
import json

import pytest

from stagefinder import db, scoring as S
from stagefinder.dedup import upsert_job
from stagefinder.models import Fact, RawJob, add_terms, create_campaign, load_campaign

FACTS = [Fact(1, "skill", "Python", "", ["Python"], "", "")]


@pytest.fixture
def conn(tmp_db):
    c = db.connect(tmp_db); db.migrate(c); return c


def _seed(conn, titles, threshold=70):
    cid = create_campaign(conn, "c", "stage data science", "FR", score_threshold=threshold)
    ids = []
    for i, t in enumerate(titles):
        r = upsert_job(conn, RawJob("indeed", f"k{i}", t, f"Co{i}", "Paris",
                                    f"https://x/{i}", "d"), cid, "k", "l")
        conn.execute("UPDATE jobs SET status='enriched', full_description=?, "
                     "enriched=? WHERE id=?",
                     (f"{t} python données", json.dumps({"missions": t, "requirements": [],
                                                         "tech_stack": [], "team_context": ""}),
                      r.job_id))
        ids.append(r.job_id)
    conn.commit()
    return load_campaign(conn, cid), ids


def test_rule_prefilter():
    assert S.rule_prefilter("stage data python", ["data"], ["senior"])
    assert not S.rule_prefilter("stage data senior", ["data"], ["senior"])
    assert not S.rule_prefilter("stage rh", ["data"], [])
    assert S.rule_prefilter("n'importe quoi", [], [])  # sans must_have : tout passe


def test_score_pending_writes_scores_and_statuses(conn, fake_llm):
    camp, ids = _seed(conn, ["Stage Data Scientist", "Stage Data Analyst", "Stage RH"])
    add_terms(conn, camp.id, "exclude", ["RH"])
    fake_llm.responses = [json.dumps([
        {"index": 0, "score": 85, "reasons": ["ML", "Python"]},
        {"index": 1, "score": 40, "reasons": ["peu de ML"]},
    ])]
    c = S.score_pending(conn, fake_llm, camp, FACTS)
    assert c == {"scored": 1, "skipped": 2, "failed": 0}
    st = {r["id"]: r["status"] for r in conn.execute("SELECT id,status FROM jobs")}
    assert st[ids[0]] == "scored" and st[ids[1]] == "skipped" and st[ids[2]] == "skipped"
    sc = conn.execute("SELECT score, reasons FROM scores WHERE job_id=?", (ids[0],)).fetchone()
    assert sc["score"] == 85 and json.loads(sc["reasons"]) == ["ML", "Python"]
    assert "stage data science" in fake_llm.prompts[0][0]  # l'intent pilote le prompt


def test_missing_index_in_llm_answer_is_failed_not_kept(conn, fake_llm):
    camp, ids = _seed(conn, ["A", "B"])
    fake_llm.responses = [json.dumps([{"index": 0, "score": 90, "reasons": []}])]
    c = S.score_pending(conn, fake_llm, camp, FACTS)
    assert c["scored"] == 1 and c["failed"] == 1
    row = conn.execute("SELECT status, failure_reason FROM jobs WHERE id=?", (ids[1],)).fetchone()
    assert row["status"] == "failed_score" and row["failure_reason"]


def test_llm_error_marks_batch_failed(conn, fake_llm):
    from stagefinder.llm import LLMError
    camp, ids = _seed(conn, ["A"])

    def boom():
        raise LLMError("x")
    fake_llm.responses = [boom]
    c = S.score_pending(conn, fake_llm, camp, FACTS)
    assert c == {"scored": 0, "skipped": 0, "failed": 1}


def test_batches(conn, fake_llm):
    camp, ids = _seed(conn, [f"T{i}" for i in range(7)])
    fake_llm.responses = [
        json.dumps([{"index": i, "score": 80, "reasons": []} for i in range(5)]),
        json.dumps([{"index": i, "score": 80, "reasons": []} for i in range(2)]),
    ]
    assert S.score_pending(conn, fake_llm, camp, FACTS, batch_size=5)["scored"] == 7
    assert fake_llm.calls == 2
```

- [ ] **Step 2 : Lancer, vérifier l'échec**

Run : `python -m pytest tests/test_scoring.py -v`
Expected : FAIL, `ModuleNotFoundError`

- [ ] **Step 3 : Écrire `stagefinder/scoring.py`**

```python
from __future__ import annotations

import json
import sqlite3

from stagefinder.db import now
from stagefinder.llm import BudgetExceeded, LLMError
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
        except BudgetExceeded:
            raise
        except LLMError as e:
            for r in batch:
                _set(conn, r["id"], "failed_score", f"llm: {e}")
                counters["failed"] += 1
            conn.commit()
            continue
        by_index = {int(m["index"]): m for m in answer
                    if isinstance(m, dict) and "index" in m and "score" in m}
        for k, r in enumerate(batch):
            m = by_index.get(k)
            if m is None:
                _set(conn, r["id"], "failed_score", "indice absent de la réponse LLM")
                counters["failed"] += 1
                continue
            score = max(0, min(100, int(m["score"])))
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
```

- [ ] **Step 4 : Lancer, vérifier le succès**

Run : `python -m pytest tests/test_scoring.py -v`
Expected : 5 PASS

- [ ] **Step 5 : Commit**

```bash
git add stagefinder/scoring.py tests/test_scoring.py
git commit -m "feat: rule prefilter and intent-driven llm scoring

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 9 : Lettres — angle, rédaction, contrôle, docx

**Files:**
- Create: `stagefinder/letters.py`
- Test: `tests/test_letters.py`

**Interfaces:**
- Consumes: `profile.format_facts`, `profile.facts_by_ids`, `enrich.detect_language`, `dedup.normalize_title`, `dedup.normalize_company`, `LLMClient.chat`, `LLMClient.chat_json`
- Produces:
  - `BLACKLIST: dict[str, list[str]]` — formules interdites par langue
  - `build_angle(llm, job: sqlite3.Row, facts, reasons: list[str], language: str, instruction: str = "") -> dict`
  - `write_letter(llm, job, angle: dict, cited: list[Fact], language: str, instruction: str = "") -> str`
  - `validate_letter(body: str, job, cited: list[Fact], job_tech: list[str], language: str) -> list[str]`
  - `render_docx(body: str, path: Path) -> None`
  - `generate_for_job(conn, llm, job_id, campaign, facts, letters_dir, instruction="", language=None) -> int` — retourne `letters.id` ; statut → `letter_ready` ou `failed_letter`
  - `generate_pending(conn, llm, campaign, facts, letters_dir) -> dict` — `{"letters": n, "failed": m}`

- [ ] **Step 1 : Écrire les tests**

`tests/test_letters.py` :

```python
import json
from pathlib import Path

import pytest

from stagefinder import db, letters as L
from stagefinder.dedup import upsert_job
from stagefinder.models import Fact, RawJob, create_campaign, load_campaign

FACTS = [
    Fact(1, "project", "Agent de veille", "Collecte et synthèse d'articles", ["LangChain", "FAISS"],
         "10 000 articles traités", "2025"),
    Fact(2, "skill", "Python et SQL", "", ["Python", "SQL"], "", ""),
]
ANGLE = {"language": "fr", "company_hooks": ["gouvernance des données"],
         "pairs": [{"requirement": "qualité des données", "fact_id": 1,
                    "why": "dédoublonnage de 10 000 articles"}],
         "gaps": ["Talend"], "opening_angle": "données industrielles"}

GOOD = ("Madame, Monsieur,\n\nVotre offre de Stage Data Scientist chez Criteo porte sur la gouvernance "
        "des données. " + "Le nettoyage de 10 000 articles scientifiques avec LangChain et FAISS m'a "
        "confronté aux mêmes problèmes de qualité. " * 15 +
        "Je serais heureux d'en discuter.\n\nCordialement")


@pytest.fixture
def conn(tmp_db):
    c = db.connect(tmp_db); db.migrate(c); return c


def _seed(conn, language="fr"):
    cid = create_campaign(conn, "c", "stage data", "FR", letter_language=language)
    r = upsert_job(conn, RawJob("indeed", "k1", "Stage Data Scientist", "Criteo", "Paris",
                                "https://x", "d"), cid, "k", "l")
    conn.execute("UPDATE jobs SET status='scored', language='fr', full_description='offre', "
                 "enriched=? WHERE id=?",
                 (json.dumps({"missions": "gouvernance", "requirements": ["Python"],
                              "tech_stack": ["Python", "Talend"], "team_context": ""}), r.job_id))
    conn.execute("INSERT INTO scores(job_id,campaign_id,score,reasons,model,scored_at) "
                 "VALUES (?,?,?,?,?,?)", (r.job_id, cid, 88, '["ML"]', "m", "t"))
    conn.commit()
    return load_campaign(conn, cid), r.job_id


def _job(conn, jid):
    return conn.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone()


def test_validate_passes_good_letter(conn):
    _, jid = _seed(conn)
    assert L.validate_letter(GOOD, _job(conn, jid), FACTS[:1], ["Python", "Talend"], "fr") == []


def test_validate_flags_tech_not_in_cited_facts(conn):
    _, jid = _seed(conn)
    body = GOOD.replace("FAISS", "Talend")
    issues = L.validate_letter(body, _job(conn, jid), FACTS[:1], ["Python", "Talend"], "fr")
    assert any("Talend" in i for i in issues)


def test_validate_flags_blacklist_language_length_company(conn):
    _, jid = _seed(conn)
    short_en = "I am a highly motivated candidate for this role at Acme."
    issues = L.validate_letter(short_en, _job(conn, jid), FACTS[:1], [], "fr")
    joined = " ".join(issues).lower()
    assert "langue" in joined and "mots" in joined and "criteo" in joined
    fr_black = GOOD.replace("Je serais heureux", "Je suis très motivé et rigoureux, je serais heureux")
    assert any("formule" in i for i in L.validate_letter(fr_black, _job(conn, jid), FACTS[:1], [], "fr"))


def test_validate_flags_unknown_number(conn):
    _, jid = _seed(conn)
    body = GOOD.replace("10 000 articles", "10 000 articles et 250 modèles")
    assert any("250" in i for i in L.validate_letter(body, _job(conn, jid), FACTS[:1], [], "fr"))


def test_generate_for_job_success(conn, fake_llm, tmp_path):
    camp, jid = _seed(conn)
    fake_llm.responses = [json.dumps(ANGLE), GOOD]
    lid = L.generate_for_job(conn, fake_llm, jid, camp, FACTS, tmp_path)
    row = conn.execute("SELECT * FROM letters WHERE id=?", (lid,)).fetchone()
    assert row["version"] == 1 and row["language"] == "fr" and row["body"] == GOOD
    assert json.loads(row["warnings"]) == []
    assert Path(row["file_path"]).exists()
    assert _job(conn, jid)["status"] == "letter_ready"
    assert fake_llm.calls == 2
    # la rédaction ne reçoit que les faits cités
    assert "[1]" in fake_llm.prompts[1][1] and "[2]" not in fake_llm.prompts[1][1]


def test_generate_retries_once_then_keeps_warnings(conn, fake_llm, tmp_path):
    camp, jid = _seed(conn)
    bad = GOOD.replace("FAISS", "Talend")
    fake_llm.responses = [json.dumps(ANGLE), bad, bad]
    lid = L.generate_for_job(conn, fake_llm, jid, camp, FACTS, tmp_path)
    w = json.loads(conn.execute("SELECT warnings FROM letters WHERE id=?", (lid,)).fetchone()[0])
    assert w and fake_llm.calls == 3
    assert "Talend" in fake_llm.prompts[2][1]  # la reprise reçoit les écarts
    assert _job(conn, jid)["status"] == "letter_ready"


def test_generate_versions_and_language_override(conn, fake_llm, tmp_path):
    camp, jid = _seed(conn)
    fake_llm.responses = [json.dumps(ANGLE), GOOD]
    L.generate_for_job(conn, fake_llm, jid, camp, FACTS, tmp_path)
    en = ("Dear Hiring Manager,\n\nYour Stage Data Scientist offer at Criteo focuses on data governance. "
          + "Cleaning 10 000 scientific articles with LangChain and FAISS exposed me to the same data "
            "quality issues. " * 15 + "Best regards")
    fake_llm.responses = [json.dumps({**ANGLE, "language": "en"}), en]
    lid2 = L.generate_for_job(conn, fake_llm, jid, camp, FACTS, tmp_path,
                              instruction="plus court", language="en")
    row = conn.execute("SELECT version, language FROM letters WHERE id=?", (lid2,)).fetchone()
    assert row["version"] == 2 and row["language"] == "en"
    assert "plus court" in fake_llm.prompts[-1][1]


def test_auto_language_follows_job(conn, fake_llm, tmp_path):
    camp, jid = _seed(conn, language="auto")
    fake_llm.responses = [json.dumps(ANGLE), GOOD]
    lid = L.generate_for_job(conn, fake_llm, jid, camp, FACTS, tmp_path)
    assert conn.execute("SELECT language FROM letters WHERE id=?", (lid,)).fetchone()[0] == "fr"


def test_generate_pending_counts_and_llm_failure(conn, fake_llm, tmp_path):
    from stagefinder.llm import LLMError
    camp, jid = _seed(conn)

    def boom():
        raise LLMError("x")
    fake_llm.responses = [boom]
    assert L.generate_pending(conn, fake_llm, camp, FACTS, tmp_path) == {"letters": 0, "failed": 1}
    assert _job(conn, jid)["status"] == "failed_letter"


def test_render_docx(tmp_path):
    p = tmp_path / "l.docx"
    L.render_docx("Ligne 1\n\nLigne 2", p)
    from docx import Document
    assert [x.text for x in Document(p).paragraphs] == ["Ligne 1", "Ligne 2"]
```

- [ ] **Step 2 : Lancer, vérifier l'échec**

Run : `python -m pytest tests/test_letters.py -v`
Expected : FAIL, `ModuleNotFoundError`

- [ ] **Step 3 : Écrire `stagefinder/letters.py`**

```python
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
```

- [ ] **Step 4 : Lancer, vérifier le succès**

Run : `python -m pytest tests/test_letters.py -v`
Expected : 11 PASS. Si `test_validate_passes_good_letter` échoue sur la longueur, ajuster le multiplicateur `* 12` dans `GOOD` pour tomber entre 250 et 350 mots — c'est la fixture qu'on ajuste, pas les bornes.

- [ ] **Step 5 : Commit**

```bash
git add stagefinder/letters.py tests/test_letters.py
git commit -m "feat: two-step fact-grounded letter generation with validation

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 10 : Pipeline, runs, CLI et test d'idempotence

**Files:**
- Create: `stagefinder/pipeline.py`, `stagefinder/cli.py`
- Test: `tests/test_pipeline.py`, `tests/test_cli.py`

**Interfaces:**
- Consumes: tout ce qui précède
- Produces:
  - `pipeline.discover(conn, campaign, sources: list[Source], limit: int = 20, workers: int = 4) -> dict` — `{"found": n, "new": m, "duplicates": k}`
  - `pipeline.run_campaign(conn, settings, campaign_id, sources, llm, fetch=enrich.fetch_page, run_id: int | None = None, limit: int = 20) -> int` — retourne `runs.id`
  - `cli.main(argv: list[str] | None = None) -> int`

- [ ] **Step 1 : Écrire les tests du pipeline**

`tests/test_pipeline.py` :

```python
import json

import pytest

from stagefinder import db, pipeline as PL
from stagefinder.config import Settings
from stagefinder.models import RawJob, add_terms, create_campaign, load_campaign

PAGE = "<html><body>" + "Stage data science, missions : analyse de données avec Python. " * 10 + "</body></html>"
ENRICHED = json.dumps({"missions": "analyse", "requirements": ["Python"], "tech_stack": ["Python"],
                       "team_context": ""})
FACTS_JSON = json.dumps({"facts": [{"kind": "skill", "label": "Python", "detail": "3 ans",
                                    "tech": ["Python"], "metric": "", "period": ""}]})
LETTER = ("Madame, Monsieur,\n\nVotre offre de Stage Data Scientist chez Criteo m'intéresse. "
          + "Mes trois ans de pratique de Python en analyse de données répondent à votre besoin. " * 18
          + "Cordialement")


class FakeSource:
    name = "fake"

    def __init__(self, jobs):
        self.jobs = jobs
        self.queries = []

    def supports(self, country):
        return True

    def search(self, keyword, location, country, job_type, hours_old, is_remote, limit):
        self.queries.append((keyword, location))
        return list(self.jobs)


@pytest.fixture
def conn(tmp_db):
    c = db.connect(tmp_db); db.migrate(c); return c


@pytest.fixture
def settings(tmp_path):
    cv = tmp_path / "cv.pdf"; cv.write_bytes(b"pdf")
    return Settings("k", "m", 50, cv, tmp_path / "db.sqlite", tmp_path / "letters")


@pytest.fixture(autouse=True)
def _no_pdf(monkeypatch):
    import stagefinder.profile as P
    monkeypatch.setattr(P, "extract_pdf_text", lambda p: "CV")


def _campaign(conn):
    cid = create_campaign(conn, "DS", "stage data science", "FR", score_threshold=70)
    add_terms(conn, cid, "keyword", ["stage data", "stage ml"])
    add_terms(conn, cid, "location", ["Paris", "Lyon"])
    return cid


def _jobs():
    return [RawJob("indeed", "in-a", "Stage Data Scientist", "Criteo", "Paris",
                   "https://fr.indeed.com/viewjob?jk=a", "d"),
            RawJob("linkedin", "li-1", "Data Scientist Intern (M/F)", "Criteo SA", "Paris, FR",
                   "https://www.linkedin.com/jobs/view/1", "d")]


def test_discover_builds_matrix_and_dedups(conn):
    cid = _campaign(conn)
    src = FakeSource(_jobs())
    c = PL.discover(conn, load_campaign(conn, cid), [src], limit=5, workers=2)
    assert sorted(src.queries) == sorted([(k, l) for k in ["stage data", "stage ml"] for l in ["Paris", "Lyon"]])
    assert c["found"] == 8 and c["new"] == 2 and c["duplicates"] == 1
    assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 2


def test_discover_skips_unsupported_source(conn):
    cid = _campaign(conn)
    src = FakeSource(_jobs()); src.supports = lambda c: False
    assert PL.discover(conn, load_campaign(conn, cid), [src])["found"] == 0


def _run(conn, settings, cid, llm, src):
    return PL.run_campaign(conn, settings, cid, [src], llm,
                           fetch=lambda u, timeout=15: PAGE, limit=5)


def test_run_campaign_end_to_end_and_idempotent(conn, settings, fake_llm):
    cid = _campaign(conn)
    src = FakeSource(_jobs())
    fake_llm.responses = [
        FACTS_JSON,                                    # profil
        ENRICHED,                                      # enrich (1 job canonique)
        json.dumps([{"index": 0, "score": 90, "reasons": ["Python"]}]),  # score
        json.dumps({"language": "fr", "company_hooks": [], "gaps": [],   # angle
                    "pairs": [{"requirement": "Python", "fact_id": 1, "why": "3 ans"}],
                    "opening_angle": ""}),
        LETTER,                                        # rédaction
    ]
    run_id = _run(conn, settings, cid, fake_llm, src)
    run = conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
    assert run["status"] == "done" and run["stage"] == "generate"
    counters = json.loads(run["counters"])
    assert counters["new"] == 2 and counters["enriched"] == 1
    assert counters["scored"] == 1 and counters["letters"] == 1
    assert run["llm_calls"] == 5
    assert conn.execute("SELECT COUNT(*) FROM letters").fetchone()[0] == 1
    assert conn.execute("SELECT status FROM jobs WHERE duplicate_of IS NULL").fetchone()[0] == "letter_ready"

    # ---- second run identique : aucune nouveauté, aucun appel LLM, aucune lettre ----
    before = fake_llm.calls
    run2 = _run(conn, settings, cid, fake_llm, src)
    r2 = conn.execute("SELECT * FROM runs WHERE id=?", (run2,)).fetchone()
    assert r2["status"] == "done"
    assert fake_llm.calls == before
    assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM letters").fetchone()[0] == 1
    assert json.loads(r2["counters"])["new"] == 0
    assert conn.execute("SELECT seen_count FROM jobs WHERE external_id='a'").fetchone()[0] > 1


def test_run_campaign_budget_exceeded_marks_failed(conn, settings, fake_llm):
    from stagefinder.llm import BudgetExceeded
    cid = _campaign(conn)

    def boom():
        raise BudgetExceeded("plafond")
    fake_llm.responses = [FACTS_JSON, boom]
    run_id = _run(conn, settings, cid, fake_llm, FakeSource(_jobs()))
    run = conn.execute("SELECT status, error, stage FROM runs WHERE id=?", (run_id,)).fetchone()
    assert run["status"] == "failed" and "plafond" in run["error"] and run["stage"] == "enrich"


def test_run_campaign_without_cv_fails_explicitly(conn, settings, fake_llm):
    cid = _campaign(conn)
    s = Settings("k", "m", 50, None, settings.db_path, settings.letters_dir)
    run_id = PL.run_campaign(conn, s, cid, [FakeSource([])], fake_llm)
    run = conn.execute("SELECT status, error FROM runs WHERE id=?", (run_id,)).fetchone()
    assert run["status"] == "failed" and "CV_PATH" in run["error"]


def test_resume_reuses_run_row(conn, settings, fake_llm):
    cid = _campaign(conn)
    fake_llm.responses = [FACTS_JSON]
    rid = PL.run_campaign(conn, settings, cid, [FakeSource([])], fake_llm)
    rid2 = PL.run_campaign(conn, settings, cid, [FakeSource([])], fake_llm, run_id=rid)
    assert rid == rid2
    assert conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 1
```

- [ ] **Step 2 : Écrire les tests de la CLI**

`tests/test_cli.py` :

```python
import json

from stagefinder import cli, db


def test_campaign_add_term_and_list(tmp_path, capsys, monkeypatch):
    dbp = tmp_path / "s.db"
    env = {"OPENROUTER_API_KEY": "k", "DB_PATH": str(dbp), "LETTERS_DIR": str(tmp_path)}
    monkeypatch.setattr(cli, "_env", lambda: env)

    assert cli.main(["campaign", "add", "--name", "DS", "--intent", "stage data", "--country", "fr",
                     "--job-type", "internship", "--letter-language", "en", "--hours-old", "168"]) == 0
    out = capsys.readouterr().out
    assert "campagne 1" in out

    assert cli.main(["campaign", "term", "1", "--kind", "keyword", "stage data", "intern ml"]) == 0
    assert cli.main(["campaign", "term", "1", "--kind", "location", "Paris"]) == 0
    assert cli.main(["campaign", "list"]) == 0
    out = capsys.readouterr().out
    assert "DS" in out and "FR" in out and "en" in out

    conn = db.connect(dbp)
    assert conn.execute("SELECT COUNT(*) FROM campaign_terms").fetchone()[0] == 3


def test_run_invokes_pipeline(tmp_path, monkeypatch):
    dbp = tmp_path / "s.db"
    cv = tmp_path / "cv.pdf"; cv.write_bytes(b"x")
    env = {"OPENROUTER_API_KEY": "k", "DB_PATH": str(dbp), "LETTERS_DIR": str(tmp_path),
           "CV_PATH": str(cv)}
    monkeypatch.setattr(cli, "_env", lambda: env)
    cli.main(["campaign", "add", "--name", "DS", "--intent", "i", "--country", "FR"])
    called = {}

    def fake_run(conn, settings, campaign_id, sources, llm, run_id=None, limit=20, **kw):
        called.update(campaign_id=campaign_id, sources=[s.name for s in sources],
                      run_id=run_id, limit=limit)
        conn.execute("INSERT OR REPLACE INTO runs(id,status,campaign_id) VALUES (7,'done',?)", (campaign_id,))
        conn.commit()
        return 7
    monkeypatch.setattr(cli.pipeline, "run_campaign", fake_run)

    assert cli.main(["run", "--campaign", "1", "--no-jobteaser", "--limit", "9"]) == 0
    assert called == {"campaign_id": 1, "sources": ["jobspy"], "run_id": None, "limit": 9}
    assert cli.main(["run", "--campaign", "1", "--resume", "7"]) == 0
    assert called["run_id"] == 7 and called["sources"] == ["jobspy", "jobteaser"]
```

- [ ] **Step 3 : Lancer, vérifier l'échec**

Run : `python -m pytest tests/test_pipeline.py tests/test_cli.py -v`
Expected : FAIL, `ModuleNotFoundError`

- [ ] **Step 4 : Écrire `stagefinder/pipeline.py`**

```python
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
from stagefinder.llm import BudgetExceeded
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
    except Exception as e:  # erreur globale : on la rend visible, on ne dégrade pas
        log.exception("run %s échoué à l'étape %s", run_id, stage)
        _update_run(conn, run_id, status="failed", stage=stage, error=f"{type(e).__name__}: {e}",
                    counters=json.dumps(counters), llm_calls=llm.calls, finished_at=now())
    return run_id
```

- [ ] **Step 5 : Écrire `stagefinder/cli.py`**

```python
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
    return p


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    a = build_parser().parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 6 : Lancer, vérifier le succès**

Run : `python -m pytest tests/test_pipeline.py tests/test_cli.py -v`
Expected : 8 PASS. Si `test_run_campaign_end_to_end_and_idempotent` échoue sur `llm_calls == 5`, vérifier l'ordre exact des réponses du `FakeLLM` : profil, enrich, score, angle, rédaction — une reprise de rédaction (validation échouée) consommerait une réponse de plus ; dans ce cas allonger/raccourcir `LETTER` pour tenir dans 250–350 mots plutôt que modifier le pipeline.

- [ ] **Step 7 : Suite complète**

Run : `python -m pytest -q`
Expected : tous PASS, aucun accès réseau (aucun test ne doit dépasser 2 s).

- [ ] **Step 8 : Commit**

```bash
git add stagefinder/pipeline.py stagefinder/cli.py tests/test_pipeline.py tests/test_cli.py
git commit -m "feat: campaign pipeline with runs tracking, resume and cli

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 11 : Première campagne réelle et retrait de l'ancien script

**Files:**
- Create: `.env` (non versionné), `README.md`
- Modify: `stage_finder_with_ai.py` → déplacé dans `legacy/`

- [ ] **Step 1 : Créer `.env` à partir de `.env.example`** avec une **nouvelle** clé OpenRouter (révoquer l'ancienne sur openrouter.ai/settings/keys).

- [ ] **Step 2 : Créer une campagne et la lancer avec un petit volume**

```bash
python -m stagefinder.cli campaign add --name "Stage DS France" \
  --intent "stage de fin d'études en data science / machine learning, 6 mois, France" \
  --country FR --job-type internship --hours-old 168 --letter-language fr
python -m stagefinder.cli campaign term 1 --kind keyword "stage data science" "stage machine learning" "data science internship"
python -m stagefinder.cli campaign term 1 --kind location "Paris" "Lyon" "France"
python -m stagefinder.cli campaign term 1 --kind exclude "senior" "CDI temps plein" "5 ans d'expérience"
python -m stagefinder.cli run --campaign 1 --limit 10
```

Expected : `run 1: done`, compteurs affichés, des `.docx` dans `data/letters/`. Ouvrir deux lettres et vérifier à la main : langue française, entreprise et poste cités, aucune techno inventée.

- [ ] **Step 3 : Relancer immédiatement**

```bash
python -m stagefinder.cli run --campaign 1 --limit 10
```

Expected : `new: 0`, `letters: 0`, exécution nettement plus rapide (aucun appel LLM hors profil en cache).

- [ ] **Step 4 : Archiver l'ancien script**

```bash
mkdir -p legacy && git mv stage_finder_with_ai.py legacy/ 2>/dev/null || mv stage_finder_with_ai.py legacy/
mv test_api.py legacy/
```

Puis dans `legacy/stage_finder_with_ai.py`, remplacer la ligne 22 par `OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")` pour ne jamais committer la clé.

- [ ] **Step 5 : Écrire `README.md`**

```markdown
# StageFinder v2

Recherche d'offres multi-sources, déduplication persistante, scoring IA piloté par une campagne,
lettres de motivation ancrées sur les faits du CV. Tout en local (SQLite).

## Installation
    python -m pip install -e ".[dev]"
    copy .env.example .env   # puis renseigner OPENROUTER_API_KEY et CV_PATH

## Utilisation
    python -m stagefinder.cli campaign add --name "..." --intent "..." --country FR --job-type internship
    python -m stagefinder.cli campaign term <id> --kind keyword "..." "..."
    python -m stagefinder.cli campaign term <id> --kind location "Paris"
    python -m stagefinder.cli run --campaign <id>
    python -m stagefinder.cli letters regenerate <job_id> --campaign <id> --instruction "plus court" --language en

## Tests
    python -m pytest -q

## Design
Voir `docs/superpowers/specs/2026-09-20-stagefinder-design.md`.
```

- [ ] **Step 6 : Commit**

```bash
git add README.md legacy
git commit -m "docs: readme, archive legacy script without secrets

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

## Auto-revue du plan

**Couverture du spec.** §3 architecture → Tasks 0–3, 10. §4 schéma → Task 1 (+ `company_norm`, `enriched`, écarts déclarés). §5.1 campagne/termes → Task 3 et CLI (la suggestion IA des mots-clés est dans le plan 2/2, comme annoncé). §5.2 discover parallèle → Task 10. §5.3 dédup 4 couches → Task 4. §5.4 enrich → Task 7. §5.5 score → Task 8. §5.6 lettres deux temps + contrôle + versions + régénération → Task 9. §5.7 track → table présente (Task 1) ; l'écriture se fait dans le dashboard (plan 2/2). §6 dashboard → plan 2/2. §7 erreurs → Tasks 2, 7, 8, 9, 10. §8 secrets → Tasks 0, 11. §9 tests → chaque task ; idempotence → Task 10. §10 dépendances → Task 0.

**Placeholders.** Aucun « TBD », chaque étape de code contient le code ; chaque test est écrit.

**Cohérence des types.** `Source.search(keyword, location, country, job_type, hours_old, is_remote, limit)` identique dans base.py, jobspy_src, jobteaser, FakeSource et pipeline.discover. `RawJob(source, external_id, title, company, location, url, description, posted_at)` identique partout. `upsert_job(conn, raw, campaign_id, matched_keyword, matched_location) -> UpsertResult(job_id, created, duplicate_of)` cohérent entre Task 4 et Task 10. `generate_for_job(conn, llm, job_id, campaign, facts, letters_dir, instruction, language)` identique entre Task 9 et la CLI. `enrich_job(..., page=None)` : le paramètre `page` est ajouté en Task 7 et utilisé par `enrich_pending` — les tests de `enrich_job` passent par `fetch`. `FakeLLM.chat_json` réutilise `llm.extract_json` défini en Task 2.
