from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from dotenv import load_dotenv

log: logging.Logger = logging.getLogger(__name__)

PROJECT_ROOT: Final[Path] = Path(__file__).resolve().parent.parent
DOTENV_PATH: Final[Path] = PROJECT_ROOT / ".env"


class MissingConfigError(Exception):
    pass


@dataclass(frozen=True)
class Config:
    mongo_uri: str
    mongo_db: str
    mongo_collection: str


def _require_env(key: str) -> str:
    value = os.getenv(key)
    if value is None or value == "":
        raise MissingConfigError(
            f"Environment variable {key!r} is unset or empty. Check .env file at {DOTENV_PATH}"
        )
    return value


def load_config() -> Config:
    if DOTENV_PATH.exists():
        load_dotenv(DOTENV_PATH, override=False)
        log.info("Loaded environment from %s", DOTENV_PATH)

    return Config(
        mongo_uri=_require_env("MONGO_URI"),
        mongo_db=_require_env("MONGO_DB"),
        mongo_collection=_require_env("MONGO_COLLECTION"),
    )
