from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from dotenv import load_dotenv

log: logging.Logger = logging.getLogger(__name__)

PROJECT_ROOT: Final[Path] = Path(__file__).resolve().parent.parent
DOTENV_PATH: Final[Path] = PROJECT_ROOT / ".env"

LOG_FORMAT: Final[str] = "%(asctime)s %(levelname)s %(name)s %(message)s"
DEFAULT_LOG_LEVEL: Final[str] = "INFO"
ALLOWED_LOG_LEVELS: Final[frozenset[str]] = frozenset(
    {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
)

DEFAULT_OUTPUT_DIR: Final[str] = "output"
DEFAULT_QUERY: Final[str] = ""
DEFAULT_LOCATION: Final[str] = ""

REQUIRED_MONGO_KEYS: Final[tuple[str, str, str]] = (
    "MONGO_URI",
    "MONGO_DB",
    "MONGO_COLLECTION",
)

_MONGO_SCHEMES: Final[frozenset[str]] = frozenset({"mongodb", "mongodb+srv"})

__all__ = [
    "ALLOWED_LOG_LEVELS",
    "Config",
    "ConfigValidationError",
    "DEFAULT_LOCATION",
    "DEFAULT_LOG_LEVEL",
    "DEFAULT_OUTPUT_DIR",
    "DEFAULT_QUERY",
    "DOTENV_PATH",
    "LOG_FORMAT",
    "MissingConfigError",
    "PROJECT_ROOT",
    "is_configured",
    "load_config",
]


class MissingConfigError(Exception):
    pass


class ConfigValidationError(ValueError):
    pass


@dataclass(frozen=True)
class Config:
    mongo_uri: str
    mongo_db: str
    mongo_collection: str
    log_level: str
    log_file: str | None
    output_dir: Path = Path(DEFAULT_OUTPUT_DIR)
    default_query: str = DEFAULT_QUERY
    default_location: str = DEFAULT_LOCATION

    def __post_init__(self) -> None:
        scheme = self.mongo_uri.split("://", maxsplit=1)[0].lower()
        if scheme not in _MONGO_SCHEMES:
            raise ConfigValidationError(
                f"mongo_uri must start with mongodb:// or mongodb+srv://, got {self.mongo_uri!r}"
            )
        if not self.mongo_db:
            raise ConfigValidationError("mongo_db cannot be empty")
        if not self.mongo_collection:
            raise ConfigValidationError("mongo_collection cannot be empty")
        if self.log_level not in ALLOWED_LOG_LEVELS:
            raise ConfigValidationError(
                f"log_level {self.log_level!r} not in {sorted(ALLOWED_LOG_LEVELS)}"
            )
        if self.log_file is not None and not isinstance(self.log_file, str):
            raise ConfigValidationError("log_file must be None or a non-empty string")
        if not isinstance(self.output_dir, Path):
            raise ConfigValidationError("output_dir must be a pathlib.Path")
        if not self.output_dir.is_absolute():
            object.__setattr__(
                self, "output_dir", (PROJECT_ROOT / self.output_dir).resolve()
            )
        else:
            object.__setattr__(self, "output_dir", self.output_dir.resolve())


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
    if raw is None or raw == "":
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


def _resolve_output_dir(raw: str | Path | None) -> Path:
    if raw is None or raw == "":
        return (PROJECT_ROOT / DEFAULT_OUTPUT_DIR).resolve()
    if isinstance(raw, Path):
        path = raw
    else:
        path = Path(raw)
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return path.resolve()


def _load_dotenv_if_present(dotenv_path: Path | None) -> None:
    if dotenv_path is None:
        resolved = DOTENV_PATH
    else:
        if not dotenv_path.is_absolute():
            resolved = (PROJECT_ROOT / dotenv_path).resolve()
        else:
            resolved = dotenv_path.resolve()
    if resolved.exists():
        load_dotenv(resolved, override=False)
        log.info("Loaded environment from %s", resolved)


def is_configured(*, dotenv_path: Path | None = None) -> bool:
    _load_dotenv_if_present(dotenv_path)
    for key in REQUIRED_MONGO_KEYS:
        value = os.getenv(key)
        if value is None or value == "":
            return False
    return True


def load_config(
    *,
    dotenv_path: Path | None = None,
    overrides: dict[str, Any] | None = None,
    mongo_uri: str | None = None,
    mongo_db: str | None = None,
    mongo_collection: str | None = None,
    log_level: str | None = None,
    log_file: str | None = None,
    output_dir: str | Path | None = None,
    default_query: str | None = None,
    default_location: str | None = None,
) -> Config:
    _load_dotenv_if_present(dotenv_path)

    merged: dict[str, Any] = {}
    if overrides is not None:
        merged.update(overrides)

    kw_only = {
        "mongo_uri": mongo_uri,
        "mongo_db": mongo_db,
        "mongo_collection": mongo_collection,
        "log_level": log_level,
        "log_file": log_file,
        "output_dir": output_dir,
        "default_query": default_query,
        "default_location": default_location,
    }
    for k, v in kw_only.items():
        if v is not None:
            merged[k] = v

    def get(name: str, env_keys: tuple[str, ...] | None = None, fallback: Any = None, require: bool = False) -> Any:
        if name in merged and merged[name] is not None:
            return merged[name]
        if env_keys is not None:
            for key in env_keys:
                value = os.getenv(key)
                if value is not None and value != "":
                    return value
        if require:
            if env_keys is None:
                raise MissingConfigError(f"Required field {name!r} has no environment keys")
            _require_env(env_keys[0])
        return fallback

    final_uri: Any = get("mongo_uri", ("MONGO_URI",), require=True)
    final_db: Any = get("mongo_db", ("MONGO_DB",), require=True)
    final_collection: Any = get("mongo_collection", ("MONGO_COLLECTION",), require=True)
    final_log_level: str = _normalise_log_level(get("log_level", ("LOG_LEVEL",)))
    final_log_file_raw = get("log_file", ("LOG_FILE",), fallback=None)
    final_log_file: str | None = None if (final_log_file_raw is None or final_log_file_raw == "") else str(final_log_file_raw)
    final_output_dir: Path = _resolve_output_dir(get("output_dir", fallback=None))
    final_query: str = get("default_query", fallback=DEFAULT_QUERY) or DEFAULT_QUERY
    final_location: str = get("default_location", fallback=DEFAULT_LOCATION) or DEFAULT_LOCATION

    return Config(
        mongo_uri=final_uri,
        mongo_db=final_db,
        mongo_collection=final_collection,
        log_level=final_log_level,
        log_file=final_log_file,
        output_dir=final_output_dir,
        default_query=final_query,
        default_location=final_location,
    )
