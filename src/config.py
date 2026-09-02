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

LOG_FORMAT: Final[str] = "%(asctime)s %(levelname)s %(name)s %(message)s"
DEFAULT_LOG_LEVEL: Final[str] = "INFO"
ALLOWED_LOG_LEVELS: Final[frozenset[str]] = frozenset(
    {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
)


class MissingConfigError(Exception):
    pass


@dataclass(frozen=True)
class Config:
    mongo_uri: str
    mongo_db: str
    mongo_collection: str
    log_level: str
    log_file: str | None


def _require_env(key: str) -> str:
    value = os.getenv(key)
    if value is None or value == "":
        raise MissingConfigError(
            f"Environment variable {key!r} is unset or empty. Check .env file at {DOTENV_PATH}"
        )
    return value


def _optional_env(key: str) -> str | None:
    value = os.getenv(key)
    if value is None or value == "":
        return None
    return value


def _normalise_log_level(raw: str | None) -> str:
    if raw is None:
        return DEFAULT_LOG_LEVEL
    candidate = raw.strip().upper()
    if candidate in ALLOWED_LOG_LEVELS:
        return candidate
    log.warning(
        "LOG_LEVEL %r is not one of %s - falling back to %s",
        raw,
        ", ".join(sorted(ALLOWED_LOG_LEVELS)),
        DEFAULT_LOG_LEVEL,
    )
    return DEFAULT_LOG_LEVEL


def load_config() -> Config:
    if DOTENV_PATH.exists():
        load_dotenv(DOTENV_PATH, override=False)
        log.info("Loaded environment from %s", DOTENV_PATH)

    log_level = _normalise_log_level(_optional_env("LOG_LEVEL"))
    log_file = _optional_env("LOG_FILE")

    return Config(
        mongo_uri=_require_env("MONGO_URI"),
        mongo_db=_require_env("MONGO_DB"),
        mongo_collection=_require_env("MONGO_COLLECTION"),
        log_level=log_level,
        log_file=log_file,
    )
