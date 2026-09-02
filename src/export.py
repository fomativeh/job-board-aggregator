from __future__ import annotations

import csv
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Final, Mapping, Sequence, TypedDict

from .schema import (
    URL_HASH_FIELD,
    JobListing,
    NULLABLE_FIELDS,
    apply_url_hashes,
    make_url_hash,
)

log: logging.Logger = logging.getLogger(__name__)

DEFAULT_OUTPUT_DIR: Final[str] = "output"
TIMESTAMP_FORMAT: Final[str] = "%Y%m%d_%H%M%S"
CSV_KEY_ORDER: tuple[str, ...] = (
    "title",
    "company",
    "location",
    "salary",
    "url",
    "source",
    "scraped_at",
    URL_HASH_FIELD,
)
CSV_NONE_CELL: Final[str] = ""
JSON_INDENT: Final[int] = 2


class RunOutput(TypedDict):
    csv_path: str
    json_path: str


def _run_timestamp(now: datetime | None = None) -> str:
    dt = now if now is not None else datetime.now(tz=timezone.utc)
    return dt.strftime(TIMESTAMP_FORMAT)


def _ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def _salary_to_csv_cell(salary: object) -> str:
    if salary is None:
        return CSV_NONE_CELL
    if isinstance(salary, (int, float)):
        return str(int(salary)) if isinstance(salary, float) and salary.is_integer() else str(salary)
    return str(salary)


def _cell_value(listing: Mapping[str, object], key: str) -> object:
    value = listing.get(key)
    if key == "salary":
        return _salary_to_csv_cell(value)
    if value is None and key in NULLABLE_FIELDS:
        return CSV_NONE_CELL
    return value


def write_csv(
    listings: Sequence[JobListing],
    output_dir: str = DEFAULT_OUTPUT_DIR,
    *,
    timestamp: str | None = None,
) -> str:
    out_dir = Path(output_dir).resolve()
    _ensure_dir(out_dir)
    ts = timestamp if timestamp is not None else _run_timestamp()
    csv_path = out_dir / f"job_listings_{ts}.csv"
    enriched = apply_url_hashes(list(listings))
    with csv_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(list(CSV_KEY_ORDER))
        for row in enriched:
            writer.writerow([_cell_value(row, key) for key in CSV_KEY_ORDER])
    log.info("CSV written: %s (%d rows)", csv_path, len(enriched))
    return str(csv_path)


def write_json(
    listings: Sequence[JobListing],
    output_dir: str = DEFAULT_OUTPUT_DIR,
    *,
    timestamp: str | None = None,
) -> str:
    out_dir = Path(output_dir).resolve()
    _ensure_dir(out_dir)
    ts = timestamp if timestamp is not None else _run_timestamp()
    json_path = out_dir / f"job_listings_{ts}.json"
    enriched = apply_url_hashes(list(listings))

    def _normalize_salary(salary: object) -> object:
        if salary is None or isinstance(salary, (int, float)):
            return salary
        return str(salary)

    normalized: list[dict[str, object]] = [
        {
            "title": row["title"],
            "company": row["company"],
            "location": row["location"],
            "salary": _normalize_salary(row.get("salary")),
            "url": row["url"],
            "source": row["source"],
            "scraped_at": row["scraped_at"],
            "url_hash": row["url_hash"],
            "url_hash_verified": make_url_hash(row["url"]) == row["url_hash"],
        }
        for row in enriched
    ]
    with json_path.open("w", encoding="utf-8") as fh:
        json.dump(normalized, fh, ensure_ascii=False, indent=JSON_INDENT)
        fh.write("\n")
    log.info("JSON written: %s (%d rows)", json_path, len(normalized))
    return str(json_path)


def write_both(
    listings: Sequence[JobListing],
    output_dir: str = DEFAULT_OUTPUT_DIR,
) -> RunOutput:
    shared_timestamp = _run_timestamp()
    csv_path = write_csv(listings, output_dir, timestamp=shared_timestamp)
    json_path = write_json(listings, output_dir, timestamp=shared_timestamp)
    return RunOutput(csv_path=csv_path, json_path=json_path)
