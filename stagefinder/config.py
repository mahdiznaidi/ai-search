from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from dotenv import dotenv_values, find_dotenv


@dataclass(frozen=True)
class Settings:
    openrouter_api_key: str
    llm_model: str = "openai/gpt-4o-mini"
    max_llm_calls_per_run: int = 300
    cv_path: Path | None = None
    db_path: Path = Path("data/stagefinder.db")
    letters_dir: Path = Path("data/letters")


def load_settings(env: Mapping[str, str] | None = None, env_file: str | None = None) -> Settings:
    """Lit .env puis l'environnement (l'environnement gagne). `env` remplace les deux (tests).

    Les chemins relatifs (DB_PATH, LETTERS_DIR, CV_PATH) sont résolus par rapport au
    répertoire du fichier `.env` trouvé (pas le répertoire courant), pour que la CLI se
    comporte pareil qu'on la lance depuis le dossier du projet ou d'ailleurs.
    """
    if env is None:
        if env_file is None:
            env_file = find_dotenv(usecwd=True)
        merged = {**dotenv_values(env_file), **os.environ}
    else:
        merged = dict(env)
    base = Path(env_file).resolve().parent if env_file else Path.cwd()
    key = merged.get("OPENROUTER_API_KEY")
    if not key:
        raise ValueError("OPENROUTER_API_KEY manquante (voir .env.example)")
    cv = merged.get("CV_PATH")

    def resolve(p: str) -> Path:
        pp = Path(p)
        return pp if pp.is_absolute() else base / pp

    return Settings(
        openrouter_api_key=key,
        llm_model=merged.get("LLM_MODEL", "openai/gpt-4o-mini"),
        max_llm_calls_per_run=int(merged.get("MAX_LLM_CALLS_PER_RUN", "300")),
        cv_path=resolve(cv) if cv else None,
        db_path=resolve(merged.get("DB_PATH", "data/stagefinder.db")),
        letters_dir=resolve(merged.get("LETTERS_DIR", "data/letters")),
    )
