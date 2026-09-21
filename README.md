# StageFinder

Un assistant personnel qui automatise votre recherche d'emploi : il trouve les offres
pertinentes sur plusieurs sites, les trie selon votre profil, rédige une lettre de
motivation sur mesure pour chacune, et garde une trace de vos candidatures — le tout
dans un tableau de bord simple, sur votre ordinateur.

## Ce que ça vous apporte

- 🔎 **Recherche automatique** sur plusieurs plateformes (Indeed, LinkedIn…), pour
  n'importe quel métier et n'importe quel pays — pas seulement des stages en France.
- 🎯 **Tri intelligent** : chaque offre est notée selon votre profil réel (votre CV),
  pas sur des mots-clés au hasard — les offres qui ne correspondent pas sont écartées
  automatiquement, vous ne voyez que les meilleures.
- ✍️ **Lettres de motivation personnalisées**, écrites à partir de vos vraies
  expériences et compétences (jamais inventées), prêtes à télécharger en `.docx`.
- 🔁 **Zéro doublon, zéro travail répété** : relancer une recherche ne retraite que les
  offres réellement nouvelles — pas de temps ni de coût perdu sur ce qui a déjà été fait.
- 📋 **Suivi de vos candidatures** : statut, dates, notes, au même endroit.

Vos données restent chez vous : rien n'est envoyé où que ce soit, à l'exception des
appels au service d'IA nécessaires pour trier les offres et écrire les lettres.

## Démarrage en 3 étapes

**1. Installer et configurer**

```
python -m pip install -e ".[dev]"
copy .env.example .env
```

Ouvrez le fichier `.env` créé et renseignez :
- votre clé API OpenRouter (pour l'IA — voir [openrouter.ai](https://openrouter.ai)),
- le chemin vers votre CV au format PDF.

**2. Lancer le tableau de bord**

```
streamlit run stagefinder/dashboard/app.py
```

Une page s'ouvre automatiquement dans votre navigateur.

**3. Créer une campagne de recherche et cliquer sur « Lancer »**

Dans l'écran **Campagnes** :
1. Décrivez en une phrase ce que vous cherchez (ex. *« poste d'ingénieur IA junior,
   CDI, en France »*) et choisissez le pays.
2. Cliquez sur **Suggérer avec l'IA** pour obtenir des mots-clés et des villes
   pertinents — vous pouvez décocher ceux que vous ne voulez pas.
3. Cliquez sur **Lancer**.

Quelques minutes plus tard :
- l'écran **Offres** affiche les résultats triés par pertinence,
- l'écran **Lettres** contient vos lettres de motivation prêtes à relire et à
  télécharger,
- l'écran **Run** vous montre l'avancement en direct pendant que ça tourne,
- l'écran **Suivi** vous permet de noter où en est chaque candidature.

## Perspectives

Pistes d'amélioration envisagées pour la suite :
- Envoi automatique des candidatures par email (brouillon prêt à valider avant envoi).
- Recherche programmée (par exemple chaque matin, sans action manuelle).
- Relances automatiques après quelques jours sans réponse.
- Enrichissement des lettres avec des informations publiques sur l'entreprise.
- Ouverture à davantage de plateformes d'offres d'emploi.

## Pour aller plus loin

La documentation technique (commandes en ligne de commande, architecture, schéma de
données, tests, limites connues) se trouve dans [`docs/TECHNICAL.md`](docs/TECHNICAL.md)
et dans la conception détaillée
[`docs/superpowers/specs/2026-09-20-stagefinder-design.md`](docs/superpowers/specs/2026-09-20-stagefinder-design.md).
