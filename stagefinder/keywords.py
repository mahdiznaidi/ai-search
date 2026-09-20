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
