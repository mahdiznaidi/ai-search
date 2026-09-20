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
