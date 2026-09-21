# StageFinder v2 — Documentation technique

Recherche d'offres multi-sources, déduplication persistante, scoring IA piloté par une
campagne, lettres de motivation ancrées sur les faits du CV. Tout en local (SQLite).

Pour la présentation générale et le démarrage rapide, voir [`README.md`](../README.md).
Pour la conception détaillée, voir
[`superpowers/specs/2026-09-20-stagefinder-design.md`](superpowers/specs/2026-09-20-stagefinder-design.md).

## Installation

    python -m pip install -e ".[dev]"
    copy .env.example .env   # puis renseigner OPENROUTER_API_KEY et CV_PATH

## Utilisation en ligne de commande

    python -m stagefinder.cli campaign add --name "..." --intent "..." --country FR --job-type internship
    python -m stagefinder.cli campaign term <id> --kind keyword "..." "..."
    python -m stagefinder.cli campaign term <id> --kind location "Paris"
    python -m stagefinder.cli run --campaign <id>
    python -m stagefinder.cli letters regenerate <job_id> --campaign <id> --instruction "plus court" --language en
    python -m stagefinder.cli jobs retry-failed --stage enrich|score|letter [--campaign <id>]

## Tableau de bord

    streamlit run stagefinder/dashboard/app.py

Cinq écrans : Campagnes (création, suggestion IA des mots-clés, lancement d'un run),
Run (suivi en direct), Offres (table triée par score, marquage manuel des doublons),
Lettres (lecture, régénération, export .docx), Suivi (statut des candidatures).
Le bouton « Lancer » démarre `stagefinder run` en sous-processus : fermer l'onglet du
navigateur n'interrompt pas un run en cours. Le bouton est désactivé (avec un
avertissement) tant qu'un run est déjà en cours pour la campagne.

## Suggestion IA des mots-clés

    python -m stagefinder.cli campaign suggest <id>              # affiche les suggestions
    python -m stagefinder.cli campaign suggest <id> --apply      # les ajoute (origin=ai)
    python -m stagefinder.cli campaign terms <id>                # liste les termes actuels
    python -m stagefinder.cli campaign term-toggle <term_id> --enable|--disable

## Première utilisation (ligne de commande)

1. **Créer `.env` à partir de `.env.example`** avec une **nouvelle** clé OpenRouter
   (révoquer l'ancienne sur openrouter.ai/settings/keys).

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
   Vérifier que des `.docx` sont générés dans `data/letters/` et que les lettres sont
   en français avec des données réelles de l'entreprise et du poste.

3. **Relancer immédiatement pour vérifier l'idempotence** :
   ```bash
   python -m stagefinder.cli run --campaign 1 --limit 10
   ```
   Vous devriez voir `new: 0` et `letters: 0`, avec une exécution nettement plus rapide
   (aucun appel LLM hors profil en cache).

## Architecture

```
stagefinder/
├── config.py       # réglages + secrets depuis .env
├── db.py           # schéma SQLite, migrations
├── models.py       # Campaign, RawJob, Fact, chargement de l'"enriched" JSON
├── llm.py          # client OpenRouter : retry, budget, parsing JSON défensif
├── sources/        # Indeed/LinkedIn (JobSpy), JobTeaser
├── dedup.py        # 4 couches de déduplication
├── enrich.py       # texte complet de l'offre, langue, extraction structurée
├── profile.py      # CV PDF → faits, mis en cache par hash
├── keywords.py     # suggestion IA des mots-clés/exclusions/villes
├── scoring.py      # pré-filtre par règles + notation LLM
├── letters.py      # angle → rédaction → validation → .docx
├── pipeline.py     # orchestration des étapes, table `runs`
├── cli.py
└── dashboard/       # tableau de bord Streamlit (data.py + app.py)
data/
├── stagefinder.db
└── letters/
```

Machine à états d'une offre :

```
discovered → enriched → scored → letter_ready → applied → answered | rejected
     ↘ duplicate                ↘ skipped (score < seuil)
             failed_enrich / failed_score / failed_letter (avec raison)
```

## Limites connues (v2 core)

- **Une campagne « traite » une offre en premier gagne** : le statut d'une offre
  (`jobs.status`) est global, pas par campagne. Une offre déjà scorée/traitée pour la
  campagne A n'est pas re-scorée si elle est aussi rattachée à la campagne B — seuls
  `scores` et `letters` sont réellement tenus par campagne.
- Le validateur de lettres ne peut détecter une technologie absente à la fois de
  l'offre et des faits du CV : il ne compare que ce qu'il connaît (offre + faits), pas
  un référentiel externe.
- Les sélecteurs JobTeaser sont repris tels quels du scraper existant et peuvent
  devenir obsolètes si JobTeaser change son HTML.
- Lancez la CLI/le dashboard depuis le dossier du projet, ou gardez `.env` à la racine
  du projet : les chemins relatifs (`DB_PATH`, `LETTERS_DIR`, `CV_PATH`) sont résolus
  par rapport à l'emplacement du fichier `.env` trouvé, pas par rapport au répertoire
  courant.
- Lancer un second run sur une campagne déjà en cours est refusé explicitement
  (`pipeline.run_campaign` vérifie qu'aucun run `running` n'existe déjà pour la
  campagne) ; le bouton « Lancer » du dashboard est grisé dans ce cas.

## Tests

    python -m pytest -q

## Hors périmètre actuel

Voir la section « Hors périmètre » de la conception pour ce qui n'est volontairement
pas implémenté (envoi automatique d'emails, planification, relances automatiques,
recherche web sur l'entreprise) — repris dans la section « Perspectives » du README.
