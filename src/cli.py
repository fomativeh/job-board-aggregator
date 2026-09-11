from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path
from typing import Final, Optional, Sequence
from urllib.parse import unquote_plus

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
from .scrapers.glassdoor import GlassdoorScrapeError
from .scrapers.flexjobs import FlexJobsScrapeError

log: logging.Logger = logging.getLogger(__name__)

_ANSI_RESET = "\033[0m"
_ANSI_BOLD = "\033[1m"
_ANSI_DIM = "\033[2m"
_ANSI_GREY = "\033[38;5;244m"

_LEVEL_COLORS: dict[str, str] = {
    "DEBUG": "\033[38;5;31m",
    "INFO": "\033[38;5;10m",
    "WARNING": "\033[38;5;214m",
    "ERROR": "\033[38;5;196m",
    "CRITICAL": "\033[1;38;5;196m",
}

_SOURCE_COLORS: dict[str, str] = {
    "greenhouse": "\033[38;5;45m",
    "glassdoor": "\033[38;5;208m",
    "flexjobs": "\033[38;5;87m",
    "src.scrapers.greenhouse": "\033[38;5;45m",
    "src.scrapers.glassdoor": "\033[38;5;208m",
    "src.scrapers.flexjobs": "\033[38;5;87m",
    "src.pipeline": "\033[38;5;81m",
    "src.storage": "\033[38;5;72m",
    "src.export": "\033[38;5;190m",
    "src.cli": "\033[38;5;15m",
    "src.config": "\033[38;5;145m",
    "src.http_utils": "\033[38;5;243m",
}


def _supports_color(stream: object) -> bool:
    if os.environ.get("NO_COLOR", "") != "":
        return False
    if os.environ.get("FORCE_COLOR", "") != "":
        return True
    try:
        isatty = getattr(stream, "isatty", None)
        if isatty is None:
            return False
        return bool(isatty())
    except Exception:
        return False


def _short_name(full_name: str) -> str:
    if full_name.startswith("src.scrapers."):
        return full_name.split("src.scrapers.", 1)[1]
    if full_name.startswith("src."):
        return full_name.split("src.", 1)[1]
    return full_name


class _ColoredFormatter(logging.Formatter):
    def __init__(self, fmt: str, *, use_color: bool) -> None:
        super().__init__(fmt)
        self.use_color = bool(use_color)

    def format(self, record: logging.LogRecord) -> str:
        if not self.use_color:
            record.name = _short_name(record.name)
            return super().format(record)
        level_color = _LEVEL_COLORS.get(record.levelname, "")
        source_color = ""
        for k, v in _SOURCE_COLORS.items():
            if record.name == k or record.name.startswith(k + "."):
                source_color = v
                break
        display_name = _short_name(record.name)
        colored_name = f"{source_color}{display_name}{_ANSI_RESET}"
        colored_level = f"{_ANSI_BOLD}{level_color}{record.levelname:<8}{_ANSI_RESET}"
        asctime_raw = self.formatTime(record, "%Y-%m-%d %H:%M:%S")
        colored_time = f"{_ANSI_GREY}{asctime_raw}{_ANSI_RESET}"
        msg = super().format(record)
        parts = msg.split(" ", 3)
        if len(parts) >= 4:
            body = parts[3]
        else:
            body = record.getMessage()
        return f"{colored_time} {colored_level} {colored_name} {body}"


_RULE_CHAR = "─"
_RULE_WIDTH = 78


def _banner(text: str, *, level: int = logging.INFO) -> None:
    if not log.isEnabledFor(level):
        return
    rule = _RULE_CHAR * _RULE_WIDTH
    log.log(level, "%s %s %s", _RULE_CHAR * 2, text, _RULE_CHAR * max(0, _RULE_WIDTH - len(text) - 4))


def _final_summary(
    *,
    result: PipelineResult,
    query: str,
    location: str,
) -> None:
    listings = result["listings"]
    by_source: dict[str, int] = {}
    for job in listings:
        by_source[job["source"]] = by_source.get(job["source"], 0) + 1
    _banner("RESULT SUMMARY", level=logging.INFO)
    rows = [
        ("Query", repr(query)),
        ("Location", repr(location)),
        ("Listings returned", str(len(listings))),
    ]
    for src in ("greenhouse", "glassdoor", "flexjobs"):
        rows.append((f"  - {src}", str(by_source.get(src, 0))))
    exports = result.get("exports")
    if exports is not None:
        rows.append(("CSV", str(exports.get("csv_path", ""))))
        rows.append(("JSON", str(exports.get("json_path", ""))))
    width = max(len(k) for k, _ in rows)
    for k, v in rows:
        log.info("%s%s  %s", k, " " * (width - len(k)), v)
    log.info(_RULE_CHAR * _RULE_WIDTH)


DEFAULT_LOG_LEVEL_CLI: Final[str] = ""
DEFAULT_LOG_FILE_CLI: Final[str] = ""

DESCRIPTION: Final[str] = (
    "Multi-Source Job Board Aggregator - scrape Greenhouse, Glassdoor, "
    "and FlexJobs in parallel, deduplicate by URL, persist to MongoDB, "
    "and export per-run output to CSV + JSON."
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
        "--max-listings",
        default=None,
        help="Maximum listings to keep in-memory post-dedup, before DB/export writes. Default: unlimited.",
        type=int,
        dest="max_listings",
    )
    parser.add_argument(
        "--max-pages-per-source",
        default=0,
        help="Pagination cap per source (reserved; 0 = use scraper defaults).",
        type=int,
        dest="max_pages_per_source",
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

    stream = sys.stderr
    use_color = _supports_color(stream)
    formatter = _ColoredFormatter(LOG_FORMAT, use_color=use_color)
    plain_formatter = logging.Formatter(LOG_FORMAT)

    stream_handler = logging.StreamHandler(stream=stream)
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
        file_handler.setFormatter(plain_formatter)
        root_logger.addHandler(file_handler)

    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("patchright").setLevel(logging.WARNING)
    logging.getLogger("playwright").setLevel(logging.WARNING)
    logging.getLogger("pyee").setLevel(logging.WARNING)

    _apply_log_level(final_level)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    configure_logging(cli_log_level=args.log_level, cli_log_file=args.log_file)
    _banner("JOB AGGREGATOR CLI", level=logging.INFO)
    query_raw: str = args.query if isinstance(args.query, str) else DEFAULT_QUERY
    location_raw: str = args.location if isinstance(args.location, str) else DEFAULT_LOCATION
    query_clean = unquote_plus(query_raw).strip()
    location_clean = unquote_plus(location_raw).strip()
    max_listings: Optional[int] = args.max_listings
    max_pages_per_source: int = int(args.max_pages_per_source or 0)
    try:
        cfg: Config = load_config(
            log_level=args.log_level or None,
            log_file=args.log_file or None,
            output_dir=args.output_dir or None,
            default_query=query_clean or None,
            default_location=location_clean or None,
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
        "query=%r  location=%r  max_listings=%r  max_pages_per_source=%d",
        query,
        location,
        max_listings,
        max_pages_per_source,
    )
    _banner("SCRAPING", level=logging.INFO)
    try:
        result: PipelineResult = asyncio.run(
            run_pipeline(
                query,
                location,
                config=cfg,
                max_listings=max_listings,
                max_pages_per_source=max_pages_per_source,
            )
        )
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
    except GlassdoorScrapeError as exc:
        log.error("Glassdoor scraper failed: %s", exc)
        return 4
    except FlexJobsScrapeError as exc:
        log.error("FlexJobs scraper failed: %s", exc)
        return 5
    _banner("EXPORT + FINALIZE", level=logging.INFO)
    listings = result["listings"]
    exports = result["exports"]
    if exports is not None:
        log.info("CSV  -> %s", exports["csv_path"])
        log.info("JSON -> %s", exports["json_path"])
    _final_summary(result=result, query=query, location=location)
    return 0


if __name__ == "__main__":
    sys.exit(main())
