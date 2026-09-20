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

## Première utilisation

1. **Créer `.env` à partir de `.env.example`** avec une **nouvelle** clé OpenRouter (révoquer l'ancienne sur openrouter.ai/settings/keys).

2. **Créer une campagne et la lancer avec un petit volume** :
   ```bash
   python -m stagefinder.cli campaign add --name "Stage DS France" \
     --intent "stage de fin d'études en data science / machine learning, 6 mois, France" \
     --country FR --job-type internship --hours-old 168 --letter-language fr
   python -m stagefinder.cli campaign term 1 --kind keyword "stage data science" "stage machine learning" "data science internship"
   python -m stagefinder.cli campaign term 1 --kind location "Paris" "Lyon" "France"
   python -m stagefinder.cli campaign term 1 --kind exclude "senior" "CDI temps plein" "5 ans d'expérience"
   python -m stagefinder.cli run --campaign 1 --limit 10
   ```
   Vérifier que des `.docx` sont générés dans `data/letters/` et que les lettres sont en français avec des données réelles de l'entreprise et du poste.

3. **Relancer immédiatement pour vérifier l'idempotence** :
   ```bash
   python -m stagefinder.cli run --campaign 1 --limit 10
   ```
   Vous devriez voir `new: 0` et `letters: 0`, avec une exécution nettement plus rapide (aucun appel LLM hors profil en cache).

## Tests
    python -m pytest -q

## Design
Voir `docs/superpowers/specs/2026-09-20-stagefinder-design.md`.
