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
