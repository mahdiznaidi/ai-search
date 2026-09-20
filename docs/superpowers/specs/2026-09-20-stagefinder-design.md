# StageFinder v2 — Design

Date : 2026-09-20
Statut : en attente de validation

## 1. Objectif

Remplacer le flux actuel (`stage_finder_with_ai.py` → fichier Excel → transfert manuel → workflow n8n → lettres) par un **système Python unique et centralisé** qui couvre extraction, enrichissement, scoring, génération de lettres et suivi des candidatures, piloté depuis un tableau de bord web local.

Décisions de cadrage prises avec l'utilisateur :

| Question | Décision |
|---|---|
| Rôle de n8n | Supprimé. Tout en Python. |
| Interface | Tableau de bord web local (Streamlit) + CLI |
| Autonomie | Le système s'arrête à la lettre. L'utilisateur relit, édite, postule lui-même. Pas d'envoi d'email. |
| Cadence | Lancement manuel. Pas de planificateur. |
| Périmètre métier | **Générique** : n'importe quel type de poste, n'importe quel pays/région. Rien de métier en dur dans le code. |
| Langue des lettres | Choisie explicitement par campagne : `fr`, `en` ou `auto` (suivre l'offre). |

## 2. Diagnostic du système actuel

### 2.1 Défauts de flux

- Trois systèmes (script, Excel, n8n) et deux ruptures manuelles. Aucun état partagé.
- Aucune mémoire entre les runs : la déduplication est intra-run seulement. Une offre vue la semaine précédente est rescorée et régénérée.
- Aucun endroit où stocker « postulé », « refusé », « relancé ».
- Le lien offre ↔ lettre repose sur l'ordre des lignes d'un Excel.
- ~190 recherches séquentielles avec `sleep(2)` : 30 à 60 minutes par run pour une charge purement I/O.
- Le score 0–100 calculé par l'IA sert au tri puis est jeté ; n8n génère une lettre pour un match à 52 comme pour un match à 91.
- Le CV est ré-analysé par le LLM à chaque lancement.

### 2.2 Défauts de code (constatés)

- `_scrape_jobs_safe` défini deux fois ; la seconde définition écrase la première et rend tout le code JobTeaser mort.
- `enable_jobteaser` jamais défini dans `__init__`.
- Clé API OpenRouter en clair dans le fichier. **À révoquer.**
- `max_results` / `delay` posés sur l'instance mais jamais lus par `search_stages()`.
- `primary_keywords = keywords[:8]` contredit le commentaire et le calcul de `total_searches` (barre de progression fausse).
- `self.ai_model` défini mais jamais utilisé ; trois appels codent `gpt-4o-mini` en dur.
- `json.loads()` direct sur la réponse du LLM : casse sur un bloc ```` ```json ```` ou un préambule.
- Repli silencieux : en cas d'échec IA, `_analyze_batch_with_ai` retourne tous les indices — indiscernable d'un filtrage réussi.
- Mauvais paquet installé : `jobspy` (verrouillage de tâches Redis) au lieu de `python-jobspy`. Le script ne démarre pas (`ModuleNotFoundError`).

### 2.3 Défauts des lettres produites (lettre BUTAGAZ analysée)

- Langue anglaise pour une offre française.
- Encodage cassé (`Master?s`, `Universit?`) et entités HTML non décodées (`&amp;`) dans le document final.
- Formules génériques (« highly motivated and detail-oriented », « excellent fit », « drive business growth »).
- Le lien compétence → besoin de l'offre est affirmé, jamais démontré.
- Les éléments concrets (10 000 articles, LangChain, FAISS, LaTeX) sont bons : c'est ce qu'il faut systématiser.

Cause première des lettres génériques : le générateur reçoit une description tronquée par JobSpy et un profil réduit à des listes de mots-clés. Le signal d'entrée est pauvre avant même le prompt.

## 3. Architecture

### 3.1 Principe

Une base SQLite locale est la source de vérité. Le pipeline est découpé en étapes indépendantes et **idempotentes** qui lisent et écrivent dans la base. Le dashboard est une vue sur cette base. Le pipeline tourne dans un processus séparé du dashboard.

```
discover → dedup → enrich → score → generate → [dashboard] → track
```

### 3.2 Structure du projet

```
stagefinder/
├── config.py          # réglages + secrets depuis l'environnement (.env)
├── db.py              # schéma, migrations, connexion
├── models.py          # dataclasses : Campaign, Job, Fact, Letter, Run
├── llm.py             # client OpenRouter unique : retry, parsing JSON défensif, plafond
├── sources/
│   ├── base.py        # interface Source
│   ├── jobspy_src.py  # Indeed + LinkedIn via python-jobspy
│   └── jobteaser.py   # scraper HTML (repris et réparé)
├── dedup.py           # normalisation + 4 couches
├── enrich.py          # page complète de l'offre, décodage HTML, extraction
├── profile.py         # CV PDF → dossier de faits, mis en cache par hash
├── keywords.py        # suggestion IA des mots-clés / exclusions / villes
├── scoring.py
├── letters.py         # angle → rédaction → contrôle → docx
├── pipeline.py        # orchestration des étapes, table runs
├── cli.py
└── dashboard.py       # Streamlit
data/
├── stagefinder.db
└── letters/
tests/
```

Règle : un module = une étape = une responsabilité. Cible < 200 lignes par module.

### 3.3 Interface Source

```python
class Source(Protocol):
    name: str
    def supports(self, country: str) -> bool: ...
    def search(self, keyword: str, location: str, country: str,
               job_type: str | None, hours_old: int | None, limit: int) -> list[RawJob]: ...
```

`RawJob` : `source`, `external_id`, `title`, `company`, `location`, `url`, `description`, `posted_at`.

Le pipeline n'interroge que les sources dont `supports(country)` est vrai. L'interface affiche les sources actives pour le pays choisi.

**Point à vérifier en premier à l'implémentation** : la liste des pays supportés par Indeed dans `python-jobspy` (`Country` enum). LinkedIn accepte toute localisation. JobTeaser est principalement européen.

### 3.4 Machine à états d'une offre

```
discovered → enriched → scored → letter_ready → applied → answered | rejected
     ↘ duplicate                ↘ skipped (score < seuil)
             failed_enrich / failed_score / failed_letter (avec raison)
```

Chaque étape ne traite que les offres au statut précédent. Relancer une étape est sans effet sur ce qui est déjà fait.

## 4. Modèle de données

```sql
CREATE TABLE campaigns (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  intent TEXT NOT NULL,             -- texte libre décrivant ce qui est cherché
  country TEXT NOT NULL,            -- ISO 3166-1 alpha-2
  job_type TEXT,                    -- internship | fulltime | contract | NULL
  is_remote BOOLEAN DEFAULT 0,
  hours_old INTEGER,                -- NULL = toutes périodes
  letter_language TEXT NOT NULL DEFAULT 'fr',   -- fr | en | auto
  score_threshold INTEGER NOT NULL DEFAULT 70,
  created_at TIMESTAMP, last_run_at TIMESTAMP
);

CREATE TABLE campaign_terms (
  id INTEGER PRIMARY KEY,
  campaign_id INTEGER NOT NULL REFERENCES campaigns(id),
  term TEXT NOT NULL,
  kind TEXT NOT NULL,               -- keyword | must_have | exclude | location
  origin TEXT NOT NULL,             -- ai | user
  enabled BOOLEAN DEFAULT 1
);

CREATE TABLE jobs (
  id INTEGER PRIMARY KEY,
  source TEXT NOT NULL, external_id TEXT NOT NULL,
  fingerprint TEXT NOT NULL,
  title TEXT, company TEXT, location TEXT, url TEXT,
  description TEXT,                 -- courte, issue de la recherche
  full_description TEXT,            -- complète, issue de enrich
  language TEXT,                    -- langue détectée de l'offre
  posted_at TIMESTAMP,
  status TEXT NOT NULL DEFAULT 'discovered',
  failure_reason TEXT,
  duplicate_of INTEGER REFERENCES jobs(id),
  first_seen_at TIMESTAMP, last_seen_at TIMESTAMP,
  seen_count INTEGER DEFAULT 1,
  UNIQUE(source, external_id)
);
CREATE INDEX idx_jobs_fingerprint ON jobs(fingerprint);
CREATE INDEX idx_jobs_status ON jobs(status);

CREATE TABLE job_campaigns (
  job_id INTEGER REFERENCES jobs(id),
  campaign_id INTEGER REFERENCES campaigns(id),
  matched_keyword TEXT, matched_location TEXT,
  PRIMARY KEY (job_id, campaign_id)
);

CREATE TABLE scores (
  job_id INTEGER NOT NULL REFERENCES jobs(id),
  campaign_id INTEGER NOT NULL REFERENCES campaigns(id),
  score INTEGER, reasons TEXT,      -- JSON
  model TEXT, scored_at TIMESTAMP,
  PRIMARY KEY (job_id, campaign_id) -- une offre peut être scorée différemment par deux campagnes
);

CREATE TABLE facts (
  id INTEGER PRIMARY KEY,
  cv_hash TEXT NOT NULL,
  kind TEXT NOT NULL,               -- experience | project | skill | education | achievement
  label TEXT, detail TEXT,
  tech TEXT,                        -- JSON list
  metric TEXT, period TEXT
);

CREATE TABLE letters (
  id INTEGER PRIMARY KEY,
  job_id INTEGER NOT NULL REFERENCES jobs(id),
  campaign_id INTEGER NOT NULL REFERENCES campaigns(id),
  version INTEGER NOT NULL DEFAULT 1,
  language TEXT NOT NULL,
  angle TEXT,                       -- JSON produit au temps 1
  body TEXT,                        -- markdown
  file_path TEXT,                   -- .docx
  warnings TEXT,                    -- JSON : contrôles non passés
  model TEXT, created_at TIMESTAMP,
  UNIQUE(job_id, campaign_id, version)
);

CREATE TABLE applications (
  job_id INTEGER PRIMARY KEY REFERENCES jobs(id),
  letter_id INTEGER REFERENCES letters(id),
  applied_at TIMESTAMP, channel TEXT,
  outcome TEXT,                     -- pending | interview | rejected | offer
  notes TEXT
);

CREATE TABLE runs (
  id INTEGER PRIMARY KEY,
  campaign_id INTEGER REFERENCES campaigns(id),
  status TEXT NOT NULL,             -- running | done | failed | cancelled
  stage TEXT,                       -- discover | enrich | score | generate
  counters TEXT,                    -- JSON
  llm_calls INTEGER DEFAULT 0,
  started_at TIMESTAMP, finished_at TIMESTAMP,
  error TEXT
);
```

Remarques :
- La dédup est **globale**, pas par campagne. Une offre appartient à N campagnes via `job_campaigns`.
- `applications` est séparée de `jobs` : c'est de l'information produite par l'utilisateur, jamais écrasée par un run.
- `letters` est versionnée ; régénérer crée une ligne, n'en détruit pas.
- `facts` est indexée par hash du PDF : un CV inchangé n'est jamais ré-analysé.

## 5. Étapes du pipeline

### 5.1 Campagne et mots-clés (`keywords.py`)

Flux dans le dashboard :

1. L'utilisateur écrit `intent`, choisit `country`, `job_type`, `letter_language`, éventuellement des villes/régions (stockées comme `campaign_terms` de kind `location`).
2. Le LLM reçoit `intent` + pays + faits du CV et propose : mots-clés **dans la langue locale du marché** (ex. `Praktikum Machine Learning` pour DE), termes d'exclusion, villes pertinentes.
3. L'utilisateur coche / décoche / ajoute. Chaque terme est stocké avec `origin = ai | user`.
4. La campagne est enregistrée et réutilisable. La suggestion IA n'est relancée qu'à la demande.

La langue des mots-clés (pilotée par le pays) et la langue des lettres (`letter_language`) sont deux réglages indépendants.

### 5.2 Discover

- Matrice = `keyword ∈ terms(kind=keyword, enabled)` × `location ∈ terms(kind=location, enabled)`. Cible : quelques dizaines de couples, pas 190.
- Exécution parallèle : `ThreadPoolExecutor` borné (4–6 threads) avec jitter. Pas de `sleep` fixe.
- Chaque `RawJob` passe par `dedup.upsert()` immédiatement.

### 5.3 Dedup (`dedup.py`)

Quatre couches, appliquées à l'écriture :

1. **Identité source** : `(source, external_id)`. L'ID est extrait de l'URL (Indeed `jk=`, LinkedIn `/jobs/view/<id>`). `INSERT … ON CONFLICT DO UPDATE SET last_seen_at, seen_count = seen_count + 1`.
2. **Empreinte** : `sha1(normalize_company(company) | normalize_title(title))`. Détecte la même offre sur deux plateformes. `location` est **exclue** volontairement (formats incompatibles entre plateformes).
   - `normalize_title` : minuscules, retrait du bruit RH par langue (`H/F`, `F/H`, `(m/w/d)`, `(M/F/D)`, `(H/M)`), des durées (`6 mois`), années, préfixes `Stage -`, suffixes de ville ; tokens triés.
   - `normalize_company` : retrait des formes juridiques (`SAS`, `SA`, `SARL`, `GmbH`, `Ltd`, `Inc`, `Group`, `France`) et suffixes d'agrégateur (`via Indeed`).
3. **Quasi-doublons** : `rapidfuzz.fuzz.token_set_ratio(title_norm_a, title_norm_b) >= 90`, comparé **uniquement** entre offres de même `normalize_company`. Le doublon reçoit `duplicate_of` et `status = 'duplicate'`.
4. **Republications** : couvertes par la couche 2 ; `seen_count` et `last_seen_at` sont mis à jour, rien n'est supprimé.

Arbitrage : seuils conservateurs. En cas de doute on garde les deux ; le dashboard offre « marquer comme doublon de… » pour fusion manuelle.

### 5.4 Enrich (`enrich.py`)

- Pour chaque offre `discovered` non doublon : GET de `url`, extraction du texte principal, **décodage des entités HTML**, détection de langue, extraction structurée par LLM (missions, stack, contexte équipe) stockée dans `full_description` / champs JSON.
- Parallèle, borné, timeout par requête. Échec → `failed_enrich` + raison, le run continue.

### 5.5 Score (`scoring.py`)

- Pré-filtre gratuit à base de règles : `must_have` (au moins un) et `exclude` (aucun) issus de `campaign_terms`.
- Puis LLM par lots de 5, prompt construit depuis `campaign.intent` + faits du CV. Sortie JSON stricte : `[{index, score, reasons[]}]`.
- `score < campaign.score_threshold` → `skipped`. Sinon `scored`.

### 5.6 Generate (`letters.py`)

Uniquement pour les offres `scored`.

**Temps 1 — angle** (un appel, sortie JSON) :
```json
{
  "language": "fr",
  "company_hooks": ["..."],
  "pairs": [{"requirement": "...", "fact_id": 7, "why": "..."}],
  "gaps": ["..."],
  "opening_angle": "..."
}
```
Entrées : offre enrichie, `reasons` du scoring, dossier de faits complet, `letter_language` (si `auto`, = `jobs.language`).

**Temps 2 — rédaction** (un appel) : reçoit l'angle et **seulement** les faits cités. Contraintes : 250–350 mots, nom de l'entreprise et intitulé exact du poste présents, liste noire de formules, interdiction de citer un fait hors liste.

**Contrôle** (sans LLM) :

| Contrôle | Échec |
|---|---|
| Toute techno / chiffre présent dans le texte existe dans les faits cités | régénération avec liste des écarts |
| Aucune formule de la liste noire | régénération |
| Entreprise + intitulé présents | régénération |
| 250 ≤ mots ≤ 350 | régénération |
| Langue détectée = langue demandée | régénération |

Une seule reprise. Ensuite `letter_ready` avec `warnings` renseigné si des contrôles restent en échec.

**Sortie** : `letters.body` (markdown) + `.docx` généré via `python-docx` à partir d'un gabarit dérivé de `cover letter.docx`, écrit dans `data/letters/<company>_<title>_v<n>.docx`.

**Régénération depuis le dashboard** : consigne libre + langue optionnelle → nouvelle version.

### 5.7 Track

Le dashboard écrit dans `applications`. Statuts d'offre `applied`, `answered`, `rejected` découlent de `applications.outcome`.

## 6. Dashboard (`dashboard.py`)

| Écran | Contenu |
|---|---|
| Campagnes | liste, création/édition, suggestion IA des termes, bouton Lancer, sources actives pour le pays |
| Run | état du run courant depuis `runs` : étape, compteurs, erreurs, coût LLM |
| Offres | table triée par score ; facettes campagne / statut / entreprise / ville ; détail ; « marquer doublon de… » |
| Lettres | lecture, édition manuelle, régénération avec consigne et langue, export docx, historique des versions, warnings |
| Suivi | candidatures par `outcome`, dates, notes |

Le bouton Lancer démarre `python -m stagefinder.cli run --campaign <id>` en sous-processus. Le dashboard ne fait jamais de travail long lui-même.

## 7. Gestion des erreurs

Principe : **aucun repli silencieux qui ressemble à un succès.**

- **Erreur par offre** (page introuvable, HTML inattendu, JSON LLM illisible après reprise) → statut `failed_*` + `failure_reason`, le run continue. Visible dans le dashboard.
- **Erreur globale** (401, quota, base verrouillée) → `runs.status = failed`, `runs.error` explicite, arrêt propre.
- **Retry** : uniquement sur 429, 5xx, timeout ; backoff exponentiel ; 3 tentatives. Jamais sur 400/401/403.
- **Plafond LLM** : `config.max_llm_calls_per_run`. Dépassé → arrêt, `runs.error = "budget exceeded"`.
- **Reprise** : `cli run --resume <run_id>` repart des statuts en base.
- **Parsing LLM** (`llm.py`) : extraction du premier bloc JSON valide (avec ou sans ```` ``` ````), une seconde tentative avec message d'erreur injecté, puis échec explicite.

## 8. Configuration et secrets

- `.env` (non versionné) : `OPENROUTER_API_KEY`, `LLM_MODEL`, `MAX_LLM_CALLS_PER_RUN`, `CV_PATH`, `DB_PATH`.
- `config.py` lit l'environnement ; aucune valeur sensible dans le code.
- La clé actuellement en clair dans `stage_finder_with_ai.py` doit être révoquée.

## 9. Tests

**Fonctions pures, tabulaires (priorité)** :
- `normalize_title` / `normalize_company` : cas FR, DE, EN, ES.
- Extraction `external_id` depuis URLs Indeed / LinkedIn (avec et sans paramètres).
- Dédup : même offre Indeed + LinkedIn → fusion ; deux postes distincts même entreprise → pas de fusion ; republication → `seen_count` incrémenté.
- Validateur de faits : techno absente des faits cités → rejet.
- Liste noire ; compteur de mots ; détection de langue.
- Construction de la matrice de recherche depuis une campagne (termes désactivés ignorés).

**Frontière LLM mockée** dans `llm.py`. Fixtures : réponse propre, bloc ```` ```json ````, préambule bavard, JSON tronqué.

**Sources** : fixtures HTML enregistrées. Aucun appel réseau en test.

**Bout en bout** : base SQLite temporaire, source factice, LLM factice. Assertions :
1. Le pipeline produit les statuts attendus.
2. **Un second run identique ne crée aucun job, n'appelle pas le LLM, ne crée aucune lettre.**

## 10. Dépendances

`python-jobspy` (remplace `jobspy`), `pandas`, `requests`, `beautifulsoup4`, `PyPDF2`, `rapidfuzz`, `streamlit`, `python-docx`, `python-dotenv`, `langdetect` (ou équivalent), `pytest`.

## 11. Hors périmètre (v2)

- Envoi automatique d'emails.
- Planification automatique (cron).
- Relances automatiques.
- Recherche web sur l'entreprise au-delà de la page de l'offre.
- Import de l'historique Excel existant (aucun fichier présent au moment du design).

## 12. Ordre de construction suggéré

1. `db.py`, `models.py`, `config.py`, `llm.py` — socle.
2. `dedup.py` + tests — la promesse centrale.
3. `sources/` + `pipeline.discover`.
4. `profile.py` (faits) + `enrich.py`.
5. `scoring.py`.
6. `letters.py` + contrôle + docx.
7. `keywords.py`.
8. `cli.py`, table `runs`, reprise.
9. `dashboard.py`.
10. Test bout en bout d'idempotence.
