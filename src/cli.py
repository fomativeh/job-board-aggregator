from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from typing import Final, Optional, Sequence

from .config import MissingConfigError
from .http_utils import MaxRetriesExceeded
from .pipeline import run_pipeline
from .scrapers.remotive import RemotiveScrapeError

log: logging.Logger = logging.getLogger(__name__)

DEFAULT_QUERY: Final[str] = ""
DEFAULT_LOCATION: Final[str] = ""
LOG_FORMAT: Final[str] = "%(asctime)s %(levelname)s %(message)s"

DESCRIPTION: Final[str] = (
    "Multi-Source Job Board Aggregator — scrape Greenhouse, WeWorkRemotely "
    "and Remotive in parallel, deduplicate by URL, and persist to MongoDB."
)

EPILOG: Final[str] = (
    "Both --query and --location are optional. An empty --query pulls the "
    "default/latest listings from each source; an empty --location disables "
    "client-side location filters (useful for 'Remote' fields that sources "
    "already encode as a source-level keyword rather than a location column)."
)

QUERY_HELP: Final[str] = (
    "Keyword filter applied client-side to each source's listings. Matches "
    "are case-insensitive substrings against title, company, location, and "
    "any tags/department columns a source exposes. Default: empty string "
    "(no keyword filter)."
)

LOCATION_HELP: Final[str] = (
    "Location filter applied client-side to each source's location column. "
    "Case-insensitive substring match (e.g. 'Remote', 'New York', 'UK'). "
    "Remotive and Greenhouse return 'Remote' explicitly for remote rows; "
    "WeWorkRemotely uses the HQ string plus a 'remote' category tag. "
    "Default: empty string (no location filter)."
)


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
        help=LOCATION_HELP,
        type=str,
    )
    return parser


def configure_logging() -> None:
    logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)


def main(argv: Optional[Sequence[str]] = None) -> int:
    configure_logging()
    parser = build_parser()
    args = parser.parse_args(argv)
    query: str = args.query or DEFAULT_QUERY
    location: str = args.location or DEFAULT_LOCATION
    log.info("CLI args: query=%r location=%r", query, location)
    try:
        listings = asyncio.run(run_pipeline(query, location))
    except KeyboardInterrupt:
        log.warning("Interrupted by user — exiting 130")
        return 130
    except MissingConfigError as exc:
        log.error("Configuration error: %s", exc)
        return 2
    except MaxRetriesExceeded as exc:
        log.error("Scrape failed: all retries exhausted. %s", exc)
        return 3
    except RemotiveScrapeError as exc:
        log.error("Remotive scraper failed: %s", exc)
        return 4
    log.info(
        "Pipeline complete. Final listing count returned to caller: %d",
        len(listings),
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
