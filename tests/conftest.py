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
