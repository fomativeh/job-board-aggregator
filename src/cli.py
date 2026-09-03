from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path
from typing import Final, Optional, Sequence

from .config import (
    ALLOWED_LOG_LEVELS,
    Config,
    ConfigValidationError,
    DEFAULT_LOCATION,
    DEFAULT_LOG_LEVEL,
    DEFAULT_OUTPUT_DIR,
    DEFAULT_QUERY,
    LOG_FORMAT,
    MissingConfigError,
    _normalise_log_level,
    load_config,
)
from .http_utils import MaxRetriesExceeded
from .pipeline import PipelineResult, run_pipeline
from .scrapers.remotive import RemotiveScrapeError

log: logging.Logger = logging.getLogger(__name__)

DEFAULT_LOG_LEVEL_CLI: Final[str] = ""
DEFAULT_LOG_FILE_CLI: Final[str] = ""

DESCRIPTION: Final[str] = (
    "Multi-Source Job Board Aggregator - scrape Greenhouse, WeWorkRemotely "
    "and Remotive in parallel, deduplicate by URL, persist to MongoDB, and "
    "export per-run output to CSV + JSON."
)

EPILOG: Final[str] = (
    "Both flags are optional. Empty --query pulls the latest default "
    "listings; empty --location skips client-side location filtering."
)

QUERY_HELP: Final[str] = "Case-insensitive keyword filter. Matches title, company, location, and tags. Default: none."

LOCATION_HELP: Final[str] = (
    "Case-insensitive substring match against each source's location column "
    "(e.g. 'Remote', 'UK'). Bare --location with no value means 'skip "
    "location filter'. Default: none."
)

OUTPUT_DIR_HELP: Final[str] = "Where CSV + JSON exports go. Created if missing. Default: output/"

LOG_LEVEL_HELP: Final[str] = (
    "Minimum log level. Overrides LOG_LEVEL from .env. Values: DEBUG INFO "
    "WARNING ERROR CRITICAL. Default: LOG_LEVEL env or INFO."
)

LOG_FILE_HELP: Final[str] = (
    "Optional log file path (additive with console). Overrides LOG_FILE "
    "from .env. Parent dirs are created if missing. Default: LOG_FILE env."
)


def _coerce_log_level(raw: str) -> str:
    if raw == "":
        return ""
    return _normalise_log_level(raw)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="job-aggregator",
        description=DESCRIPTION,
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--query",
        default=DEFAULT_QUERY,
        help=QUERY_HELP,
        type=str,
    )
    parser.add_argument(
        "--location",
        default=DEFAULT_LOCATION,
        const=DEFAULT_LOCATION,
        nargs="?",
        help=LOCATION_HELP,
        type=str,
    )
    parser.add_argument(
        "--output-dir",
        default=DEFAULT_OUTPUT_DIR,
        help=OUTPUT_DIR_HELP,
        type=str,
        dest="output_dir",
    )
    parser.add_argument(
        "--log-level",
        default=DEFAULT_LOG_LEVEL_CLI,
        help=LOG_LEVEL_HELP,
        type=str,
        dest="log_level",
    )
    parser.add_argument(
        "--log-file",
        default=DEFAULT_LOG_FILE_CLI,
        help=LOG_FILE_HELP,
        type=str,
        dest="log_file",
    )
    return parser


def _apply_log_level(level_name: str) -> None:
    level = logging.getLevelName(level_name)
    root_logger = logging.getLogger()
    root_logger.setLevel(level)
    for handler in root_logger.handlers:
        handler.setLevel(level)


def configure_logging(*, cli_log_level: str, cli_log_file: str) -> None:
    root_logger = logging.getLogger()
    if root_logger.handlers:
        return

    formatter = logging.Formatter(LOG_FORMAT)

    stream_handler = logging.StreamHandler(stream=sys.stderr)
    stream_handler.setFormatter(formatter)
    root_logger.addHandler(stream_handler)

    try:
        overrides: dict[str, object] = {}
        if cli_log_level != "":
            overrides["log_level"] = cli_log_level
        if cli_log_file != "":
            overrides["log_file"] = cli_log_file
        config = load_config(overrides=overrides) if overrides else load_config()
        final_level: str = config.log_level
        final_log_file: str | None = config.log_file
    except (MissingConfigError, ConfigValidationError):
        final_level = DEFAULT_LOG_LEVEL
        final_log_file = None

    if final_log_file:
        log_path = Path(final_log_file).resolve()
        if not log_path.parent.exists():
            log_path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(log_path, encoding="utf-8")
        file_handler.setFormatter(formatter)
        root_logger.addHandler(file_handler)
        log.info("Logging to file: %s", log_path)

    _apply_log_level(final_level)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    configure_logging(cli_log_level=args.log_level, cli_log_file=args.log_file)
    try:
        cfg: Config = load_config(
            log_level=args.log_level or None,
            log_file=args.log_file or None,
            output_dir=args.output_dir or None,
            default_query=args.query or None,
            default_location=args.location or None,
        )
    except MissingConfigError as exc:
        log.error("Configuration error: %s", exc)
        return 2
    except ConfigValidationError as exc:
        log.error("Config validation error: %s", exc)
        return 2
    query: str = cfg.default_query
    location: str = cfg.default_location
    log.info(
        "CLI args: query=%r location=%r output_dir=%r log_level=%r log_file=%r",
        query,
        location,
        str(cfg.output_dir),
        cfg.log_level,
        cfg.log_file,
    )
    try:
        result: PipelineResult = asyncio.run(run_pipeline(query, location, config=cfg))
    except KeyboardInterrupt:
        log.warning("Interrupted by user - exiting 130")
        return 130
    except MissingConfigError as exc:
        log.error("Configuration error: %s", exc)
        return 2
    except ConfigValidationError as exc:
        log.error("Config validation error: %s", exc)
        return 2
    except MaxRetriesExceeded as exc:
        log.error("Scrape failed: all retries exhausted. %s", exc)
        return 3
    except RemotiveScrapeError as exc:
        log.error("Remotive scraper failed: %s", exc)
        return 4
    listings = result["listings"]
    exports = result["exports"]
    if exports is not None:
        log.info("CSV  -> %s", exports["csv_path"])
        log.info("JSON -> %s", exports["json_path"])
    log.info(
        "Pipeline complete. Final listing count returned to caller: %d",
        len(listings),
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
