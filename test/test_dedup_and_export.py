from __future__ import annotations

import hashlib
import json as _json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

import pytest

from src.cli import _coerce_log_level
from src.config import (
    Config,
    ConfigValidationError,
    DEFAULT_LOG_LEVEL,
    DEFAULT_OUTPUT_DIR,
    MissingConfigError,
    PROJECT_ROOT,
    is_configured,
    load_config,
)
from src.http_utils import MAX_DELAY, MIN_DELAY, USER_AGENTS, build_headers, jitter
from src.schema import (
    URL_HASH_FIELD,
    JobListing,
    SalaryType,
    ValidationError,
    apply_url_hashes,
    dedup_in_memory,
    make_url_hash,
    validate_listing,
)
from src.export import write_both, write_csv, write_json
from src.storage import MongoConnectionError, Storage


SCRAPED_AT = "2026-09-03T00:00:00Z"


def _make_listing(url: str, source: str = "greenhouse", salary: SalaryType = None) -> JobListing:
    return JobListing(
        title="Software Engineer",
        company="Acme",
        location="Remote",
        salary=salary,
        url=url,
        source=source,
        scraped_at=SCRAPED_AT,
    )


def _now(timestamp: str) -> datetime:
    return datetime.strptime(timestamp, "%Y%m%d_%H%M%S").replace(tzinfo=timezone.utc)


def test_make_url_hash_deterministic_sha256() -> None:
    url = "https://example.com/job/1"
    expected = hashlib.sha256(url.encode("utf-8")).hexdigest()
    assert make_url_hash(url) == expected


def test_make_url_hash_distinguishes_inputs() -> None:
    assert make_url_hash("https://a/1") != make_url_hash("https://a/2")


def test_validate_listing_missing_required_fields() -> None:
    bad: dict[str, object] = {"title": "X", "company": "Y"}
    with pytest.raises(ValidationError):
        validate_listing(bad)


def test_validate_listing_bad_source_rejected() -> None:
    bad = _make_listing("https://ex.com/1")
    bad["source"] = "example"
    with pytest.raises(ValidationError):
        validate_listing(bad)


def test_validate_listing_bad_url_rejected() -> None:
    bad = _make_listing("mailto:foo@bar.com")
    with pytest.raises(ValidationError):
        validate_listing(bad)


def test_validate_listing_bad_scraped_at_rejected() -> None:
    bad = _make_listing("https://ex.com/1")
    bad["scraped_at"] = "not-a-date"
    with pytest.raises(ValidationError):
        validate_listing(bad)


def test_validate_listing_salary_nullable() -> None:
    good = _make_listing("https://ex.com/1", salary=None)
    validate_listing(good)
    good_str = _make_listing("https://ex.com/1", salary="$100k")
    validate_listing(good_str)


def test_apply_url_hashes_populates_missing_and_idempotent() -> None:
    l1: dict[str, Any] = dict(_make_listing("https://ex.com/1"))
    l2: dict[str, Any] = dict(_make_listing("https://ex.com/2"))
    l2[URL_HASH_FIELD] = "already-set"
    listings_raw: list[dict[str, Any]] = [l1, l2]
    listings = cast(list[JobListing], listings_raw)
    result = apply_url_hashes(listings)
    assert l1[URL_HASH_FIELD] == make_url_hash("https://ex.com/1")
    assert l2[URL_HASH_FIELD] == "already-set"
    assert result is listings


def test_dedup_in_memory_first_occurrence_wins_and_drops_counted() -> None:
    a = _make_listing("https://ex.com/a")
    a_dup = _make_listing("https://ex.com/a")
    a_dup["title"] = "Duplicate Different Title"
    b = _make_listing("https://ex.com/b")
    deduped, dropped = dedup_in_memory([a, a_dup, b])
    assert dropped == 1
    assert len(deduped) == 2
    assert deduped[0]["url"] == "https://ex.com/a"
    assert deduped[0]["title"] == "Software Engineer"
    assert deduped[1]["url"] == "https://ex.com/b"
    for d in deduped:
        assert URL_HASH_FIELD in d


def test_dedup_in_memory_no_duplicates_empty_drop() -> None:
    lsts = [_make_listing("https://ex.com/1"), _make_listing("https://ex.com/2")]
    deduped, dropped = dedup_in_memory(lsts)
    assert dropped == 0
    assert len(deduped) == 2


def test_write_csv_json_shared_timestamp(tmp_path: Path) -> None:
    l1 = _make_listing("https://ex.com/1", salary="$80k")
    l2 = _make_listing("https://ex.com/2", salary=None, source="remotive")
    ts = "20260903_001122"
    with patch("src.export._run_timestamp", return_value=ts):
        out = write_both([l1, l2], output_dir=str(tmp_path))
    expected_csv = str(tmp_path / f"job_listings_{ts}.csv")
    expected_json = str(tmp_path / f"job_listings_{ts}.json")
    assert out["csv_path"] == expected_csv
    assert out["json_path"] == expected_json
    assert Path(out["csv_path"]).exists()
    assert Path(out["json_path"]).exists()
    assert "job_listings_20260903_001122.csv" in out["csv_path"]


def test_write_csv_header_order_and_salary_null_empty(tmp_path: Path) -> None:
    lst = _make_listing("https://ex.com/1", salary=None)
    csv_path = write_csv([lst], output_dir=str(tmp_path), timestamp="t1")
    content = Path(csv_path).read_text(encoding="utf-8")
    lines = content.splitlines()
    header = lines[0].split(",")
    assert header == ["title", "company", "location", "salary", "url", "source", "scraped_at", URL_HASH_FIELD]
    row = lines[1].split(",")
    assert row[3] == ""


def test_write_json_verifies_url_hash_and_keys(tmp_path: Path) -> None:
    lst = _make_listing("https://ex.com/1", salary="$100 - $120k")
    json_path = write_json([lst], output_dir=str(tmp_path), timestamp="t2")
    data = _json.loads(Path(json_path).read_text(encoding="utf-8"))
    assert len(data) == 1
    row = data[0]
    assert set(row.keys()) >= {
        "title", "company", "location", "salary", "url", "source", "scraped_at", URL_HASH_FIELD, "url_hash_verified"
    }
    assert row["url_hash"] == make_url_hash(row["url"])
    assert row["url_hash_verified"] is True
    assert row["salary"] == "$100 - $120k"


def test_storage_connect_bad_uri_raises_mongo_connection_error() -> None:
    cfg = Config(
        mongo_uri="mongodb://invalid-host-xyz-123.invalid:27017",
        mongo_db="job_aggregator",
        mongo_collection="job_listings",
        log_level="INFO",
        log_file=None,
    )
    store = Storage(cfg)
    with pytest.raises(MongoConnectionError):
        store.connect()


def test_storage_insert_many_unique_empty_is_zero_zero() -> None:
    cfg = Config(
        mongo_uri="mongodb://localhost:27017",
        mongo_db="job_aggregator",
        mongo_collection="job_listings",
        log_level="INFO",
        log_file=None,
    )
    store = Storage(cfg)
    store._collection = None
    inserted, dups = store.insert_many_unique([])
    assert (inserted, dups) == (0, 0)


def test_storage_insert_many_unique_validation_rejects_before_connect() -> None:
    cfg = Config(
        mongo_uri="mongodb://localhost:27017",
        mongo_db="job_aggregator",
        mongo_collection="job_listings",
        log_level="INFO",
        log_file=None,
    )
    store = Storage(cfg)
    store._collection = None
    good = _make_listing("https://ex.com/1")
    bad_raw: dict[str, Any] = {k: v for k, v in good.items() if k != "title"}
    bad = cast(JobListing, bad_raw)
    with pytest.raises(ValidationError):
        store.insert_many_unique([bad])


def test_jitter_always_in_bounds_and_varies() -> None:
    samples = [jitter() for _ in range(200)]
    for s in samples:
        assert MIN_DELAY <= s <= MAX_DELAY
    assert len(set(round(s, 6) for s in samples)) > 1


def test_build_headers_rotates_user_agent() -> None:
    agents_seen: set[str] = set()
    for _ in range(200):
        h = build_headers("https://weworkremotely.com/remote-jobs/search?term=x", referer="https://weworkremotely.com/")
        agents_seen.add(h["User-Agent"])
    assert len(agents_seen) >= 2
    for ua in agents_seen:
        assert ua in USER_AGENTS


def test_build_headers_contextualizes_sec_fetch_and_referer() -> None:
    same = build_headers(
        "https://weworkremotely.com/remote-jobs/search?term=x",
        referer="https://weworkremotely.com/",
    )
    cross = build_headers(
        "https://weworkremotely.com/remote-jobs/search?term=x",
        referer="https://google.com/",
    )
    assert same["Sec-Fetch-Site"] == "same-origin"
    assert same["Referer"] == "https://weworkremotely.com/"
    assert cross["Sec-Fetch-Site"] == "cross-site"
    assert cross["Referer"] == "https://google.com/"
    for h in (same, cross):
        assert "Accept-Language" in h


# --- cli: _coerce_log_level casing + unknown fallback ---


def test_coerce_log_level_case_insensitive() -> None:
    for raw in ("debug", "DEBUG", "Debug", "INFO", "Warning", "ERROR", "critical"):
        assert _coerce_log_level(raw).upper() == raw.upper()


def test_coerce_log_level_unknown_returns_default() -> None:
    assert _coerce_log_level("") == ""
    assert _coerce_log_level("trace") == DEFAULT_LOG_LEVEL
    assert _coerce_log_level("debugg") == DEFAULT_LOG_LEVEL
    assert _coerce_log_level("INFOO") == DEFAULT_LOG_LEVEL


def test_load_config_missing_mongo_raises(tmp_path: Path) -> None:
    fake_env = tmp_path / ".env-empty"
    fake_env.write_text("", encoding="utf-8")
    saved = {k: os.environ.pop(k, None) for k in ("MONGO_URI", "MONGO_DB", "MONGO_COLLECTION")}
    try:
        with patch("src.config.DOTENV_PATH", fake_env):
            with pytest.raises(MissingConfigError):
                load_config()
    finally:
        for k, v in saved.items():
            if v is not None:
                os.environ[k] = v
            else:
                os.environ.pop(k, None)


def test_load_config_log_fields_default_safely(tmp_path: Path) -> None:
    fake_env = tmp_path / ".env-mongo-only"
    fake_env.write_text(
        "MONGO_URI=mongodb://localhost:27017\n"
        "MONGO_DB=job_aggregator\n"
        "MONGO_COLLECTION=job_listings\n",
        encoding="utf-8",
    )
    saved = {k: os.environ.pop(k, None) for k in ("MONGO_URI", "MONGO_DB", "MONGO_COLLECTION", "LOG_LEVEL", "LOG_FILE")}
    try:
        with patch("src.config.DOTENV_PATH", fake_env):
            cfg = load_config()
        assert cfg.log_level == DEFAULT_LOG_LEVEL
        assert cfg.log_file is None
    finally:
        for k, v in saved.items():
            if v is not None:
                os.environ[k] = v
            else:
                os.environ.pop(k, None)


# --- config: post_init validation, overrides priority, output_dir resolution, is_configured ---


def test_config_post_init_rejects_bad_mongo_uri_scheme() -> None:
    from pathlib import Path as _P
    good_common: dict[str, object] = {
        "mongo_db": "job_aggregator",
        "mongo_collection": "job_listings",
        "log_level": "INFO",
        "log_file": None,
        "output_dir": _P("output"),
        "default_query": "",
        "default_location": "",
    }
    with pytest.raises(ConfigValidationError):
        Config(mongo_uri="http://localhost:27017", **good_common)  # type: ignore[arg-type]
    with pytest.raises(ConfigValidationError):
        Config(mongo_uri="ftp://localhost", **good_common)  # type: ignore[arg-type]
    with pytest.raises(ConfigValidationError):
        Config(mongo_uri="mongodb://localhost:27017", mongo_db="", mongo_collection="job_listings", log_level="INFO", log_file=None, output_dir=_P("o"), default_query="", default_location="")
    with pytest.raises(ConfigValidationError):
        Config(mongo_uri="mongodb://localhost:27017", mongo_db="x", mongo_collection="", log_level="INFO", log_file=None, output_dir=_P("o"), default_query="", default_location="")
    with pytest.raises(ConfigValidationError):
        Config(mongo_uri="mongodb://localhost:27017", mongo_db="x", mongo_collection="y", log_level="trace", log_file=None, output_dir=_P("o"), default_query="", default_location="")
    ok = Config(
        mongo_uri="mongodb+srv://user:pw@example.mongodb.net/",
        mongo_db="x", mongo_collection="y", log_level="DEBUG",
        log_file=None, output_dir=_P("artifacts"), default_query="engineer", default_location="",
    )
    assert ok.mongo_uri.startswith("mongodb+srv://")
    assert ok.log_level == "DEBUG"
    assert ok.output_dir.is_absolute()


def test_load_config_overrides_win_over_env_and_defaults(tmp_path: Path) -> None:
    fake_env = tmp_path / ".env-with-log-level"
    fake_env.write_text(
        "MONGO_URI=mongodb://localhost:27017\n"
        "MONGO_DB=job_aggregator\n"
        "MONGO_COLLECTION=job_listings\n"
        "LOG_LEVEL=WARNING\n",
        encoding="utf-8",
    )
    saved = {k: os.environ.pop(k, None) for k in ("MONGO_URI", "MONGO_DB", "MONGO_COLLECTION", "LOG_LEVEL", "LOG_FILE")}
    try:
        cfg_env = load_config(dotenv_path=fake_env)
        assert cfg_env.log_level == "WARNING"
        assert cfg_env.default_query == ""
        assert cfg_env.default_location == ""
        assert str(cfg_env.output_dir).endswith(DEFAULT_OUTPUT_DIR.replace("/", "").replace("\\", ""))
        cfg_over = load_config(
            dotenv_path=fake_env,
            log_level="DEBUG",
            default_query="rust",
            default_location="EU",
            output_dir="runs",
        )
        assert cfg_over.log_level == "DEBUG"
        assert cfg_over.default_query == "rust"
        assert cfg_over.default_location == "EU"
        assert cfg_over.output_dir.is_absolute()
        assert cfg_over.output_dir.name == "runs"
        assert cfg_over.mongo_uri == cfg_env.mongo_uri
    finally:
        for k, v in saved.items():
            if v is not None:
                os.environ[k] = v
            else:
                os.environ.pop(k, None)


def test_config_output_dir_resolved_absolute_relative_to_project_root(tmp_path: Path) -> None:
    fake_env = tmp_path / ".env-mongo"
    fake_env.write_text(
        "MONGO_URI=mongodb://localhost:27017\n"
        "MONGO_DB=job_aggregator\n"
        "MONGO_COLLECTION=job_listings\n",
        encoding="utf-8",
    )
    saved = {k: os.environ.pop(k, None) for k in ("MONGO_URI", "MONGO_DB", "MONGO_COLLECTION")}
    try:
        cfg_default = load_config(dotenv_path=fake_env)
        assert cfg_default.output_dir.is_absolute()
        assert cfg_default.output_dir == (PROJECT_ROOT / DEFAULT_OUTPUT_DIR).resolve()
        cfg_artifacts = load_config(dotenv_path=fake_env, output_dir="artifacts")
        assert cfg_artifacts.output_dir.is_absolute()
        assert cfg_artifacts.output_dir == (PROJECT_ROOT / "artifacts").resolve()
        abs_p = tmp_path / "elsewhere"
        cfg_abs = load_config(dotenv_path=fake_env, output_dir=str(abs_p))
        assert cfg_abs.output_dir.is_absolute()
        assert cfg_abs.output_dir == abs_p.resolve()
    finally:
        for k, v in saved.items():
            if v is not None:
                os.environ[k] = v
            else:
                os.environ.pop(k, None)


def test_is_configured_semantics_with_custom_dotenv(tmp_path: Path) -> None:
    empty = tmp_path / ".env-empty"
    empty.write_text("", encoding="utf-8")
    full = tmp_path / ".env-mongo"
    full.write_text(
        "MONGO_URI=mongodb://localhost:27017\n"
        "MONGO_DB=job_aggregator\n"
        "MONGO_COLLECTION=job_listings\n",
        encoding="utf-8",
    )
    partial = tmp_path / ".env-partial"
    partial.write_text("MONGO_URI=mongodb://localhost:27017\nMONGO_DB=x\n", encoding="utf-8")
    saved = {k: os.environ.pop(k, None) for k in ("MONGO_URI", "MONGO_DB", "MONGO_COLLECTION")}
    try:
        assert is_configured(dotenv_path=empty) is False
        assert is_configured(dotenv_path=partial) is False
        assert is_configured(dotenv_path=full) is True
    finally:
        for k, v in saved.items():
            if v is not None:
                os.environ[k] = v
            else:
                os.environ.pop(k, None)
