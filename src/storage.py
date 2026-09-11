from __future__ import annotations

import logging
from collections import Counter
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Sequence

import pymongo
from pymongo import MongoClient, UpdateOne
from pymongo.collection import Collection
from pymongo.database import Database
from pymongo.errors import BulkWriteError, ConnectionFailure, DuplicateKeyError, InvalidURI, OperationFailure

from .config import Config
from .schema import JobListing, _url_hash

log: logging.Logger = logging.getLogger(__name__)

__all__ = [
    "BulkWriteSummary",
    "CollectionHandle",
    "connect_to_mongo",
    "DatabaseHandle",
    "get_collection",
    "StorageConnectionError",
    "StorageValidationError",
    "upsert_many_unique_by_hash",
]


class StorageConnectionError(Exception):
    pass


class StorageValidationError(ValueError):
    pass


class DatabaseHandle:
    def __init__(self, client: MongoClient, db: Database) -> None:
        self.client: MongoClient = client
        self.db: Database = db

    def close(self) -> None:
        try:
            self.client.close()
        except Exception as exc:  # noqa: BLE001
            log.warning("Ignored error closing MongoDB client: %s", exc)

    def __enter__(self) -> "DatabaseHandle":
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()


class CollectionHandle(DatabaseHandle):
    def __init__(self, db_handle: DatabaseHandle, collection: Collection) -> None:
        super().__init__(db_handle.client, db_handle.db)
        self.collection: Collection = collection


class BulkWriteSummary:
    __slots__ = ("total", "inserted", "updated", "duplicates_skipped", "failed")

    def __init__(self, total: int, inserted: int, updated: int, duplicates_skipped: int, failed: int) -> None:
        self.total = int(total)
        self.inserted = int(inserted)
        self.updated = int(updated)
        self.duplicates_skipped = int(duplicates_skipped)
        self.failed = int(failed)

    def to_dict(self) -> dict[str, int]:
        return {
            "total": self.total,
            "inserted": self.inserted,
            "updated": self.updated,
            "duplicates_skipped": self.duplicates_skipped,
            "failed": self.failed,
        }

    def __repr__(self) -> str:
        return (
            f"BulkWriteSummary(total={self.total}, inserted={self.inserted}, "
            f"updated={self.updated}, duplicates_skipped={self.duplicates_skipped}, failed={self.failed})"
        )


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def connect_to_mongo(config: Config, *, timeout_ms: int = 5000) -> DatabaseHandle:
    if not isinstance(config, Config):
        raise StorageValidationError("config must be a Config instance")
    if not isinstance(timeout_ms, int) or timeout_ms <= 0:
        raise StorageValidationError("timeout_ms must be a positive integer")
    try:
        client: MongoClient = MongoClient(
            config.mongo_uri,
            serverSelectionTimeoutMS=timeout_ms,
            connectTimeoutMS=timeout_ms,
        )
    except InvalidURI as exc:
        raise StorageValidationError(f"Invalid MongoDB URI: {exc}") from exc
    except (ConnectionFailure, OperationFailure, pymongo.errors.PyMongoError) as exc:
        raise StorageConnectionError(f"MongoDB client init failed: {exc}") from exc
    try:
        client.admin.command("ping")
    except (ConnectionFailure, OperationFailure, pymongo.errors.PyMongoError) as exc:
        try:
            client.close()
        except Exception:  # noqa: BLE001
            pass
        raise StorageConnectionError(f"MongoDB ping failed: {exc}") from exc
    db: Database = client.get_database(config.mongo_db)
    return DatabaseHandle(client, db)


def get_collection(db_handle: DatabaseHandle, *, collection_name: str | None = None, config: Config) -> CollectionHandle:
    if not isinstance(db_handle, DatabaseHandle):
        raise StorageValidationError("db_handle must be a DatabaseHandle instance")
    if not isinstance(config, Config):
        raise StorageValidationError("config must be a Config instance")
    if collection_name is None:
        resolved_name = config.mongo_collection
    else:
        if not isinstance(collection_name, str) or not collection_name:
            raise StorageValidationError("collection_name must be a non-empty string or None")
        resolved_name = collection_name
    collection: Collection = db_handle.db.get_collection(resolved_name)
    handle = CollectionHandle(db_handle, collection)
    _ensure_indexes(handle)
    return handle


def _ensure_indexes(handle: CollectionHandle) -> None:
    url_hash_index = [("url_hash", pymongo.ASCENDING)]
    handle.collection.create_index(url_hash_index, unique=True, name="url_hash_unique")
    handle.collection.create_index([("source", pymongo.ASCENDING)], name="source_idx")
    handle.collection.create_index([("scraped_at", pymongo.DESCENDING)], name="scraped_at_idx")
    handle.collection.create_index(
        [("title", pymongo.TEXT), ("company", pymongo.TEXT), ("location", pymongo.TEXT)],
        name="text_search",
    )


def upsert_many_unique_by_hash(handle: CollectionHandle, listings: Sequence[JobListing]) -> BulkWriteSummary:
    if not isinstance(handle, CollectionHandle):
        raise StorageValidationError("handle must be a CollectionHandle instance")
    if not isinstance(listings, Sequence):
        raise StorageValidationError("listings must be a list/Sequence of JobListing dicts")

    total = len(listings)
    if total == 0:
        return BulkWriteSummary(0, 0, 0, 0, 0)

    if len(listings) != len(set(lst for lst in listings)):
        counter = Counter(_url_hash(lst) for lst in listings)
        duplicates_in_batch = sum(1 for v in counter.values() if v > 1)
        log.warning(
            "upsert_many_unique_by_hash received %d listings with %d duplicate url_hash values within the batch - deduping before write",
            total,
            duplicates_in_batch,
        )

    deduped: list[JobListing] = []
    seen_hashes: set[str] = set()
    for lst in listings:
        if not isinstance(lst, Mapping):
            log.warning("Skipping non-Mapping listing entry %r", type(lst).__name__)
            continue
        digest = _url_hash(lst)
        if digest in seen_hashes:
            continue
        seen_hashes.add(digest)
        deduped.append(lst)
    if not deduped:
        return BulkWriteSummary(total, 0, 0, 0, 0)

    now = _now_utc()
    requests: list[UpdateOne] = []
    for lst in deduped:
        digest = _url_hash(lst)
        doc: dict[str, Any] = dict(lst)
        doc["url_hash"] = digest
        doc["updated_at"] = now
        requests.append(
            UpdateOne(
                {"url_hash": digest},
                {"$set": doc, "$setOnInsert": {"inserted_at": now}},
                upsert=True,
            )
        )

    inserted = 0
    updated = 0
    duplicates_skipped = 0
    failed = 0

    try:
        result = handle.collection.bulk_write(requests, ordered=False)
    except BulkWriteError as exc:
        upserted_count = getattr(exc, "upserted_count", 0) or 0
        matched_count = getattr(exc, "matched_count", 0) or 0
        modified_count = getattr(exc, "modified_count", 0) or 0
        inserted = max(0, upserted_count)
        updated = max(0, matched_count - 0) + max(0, modified_count)
        write_errors: Iterable[Mapping[str, Any]] = exc.details.get("writeErrors", []) if exc.details else []
        duplicate_codes = (11000,)
        for we in write_errors:
            code = we.get("code") if isinstance(we, Mapping) else None
            if code in duplicate_codes:
                duplicates_skipped += 1
            else:
                failed += 1
                log.warning("Upsert error code=%s message=%s", code, we.get("errmsg") if isinstance(we, Mapping) else we)
        log.warning(
            "Mixed result from bulk_write: inserted=%d updated=%d duplicates=%d failed=%d",
            inserted,
            updated,
            duplicates_skipped,
            failed,
        )
    except (ConnectionFailure, OperationFailure, pymongo.errors.PyMongoError) as exc:
        raise StorageConnectionError(f"MongoDB bulk_write failed: {exc}") from exc
    else:
        inserted = int(result.upserted_count or 0)
        updated = int(result.modified_count or 0) + int(result.matched_count or 0)

    duplicates_skipped += max(0, len(deduped) - (inserted + updated + failed + max(0, duplicates_skipped)))
    expected = inserted + updated + failed + duplicates_skipped
    if expected != len(deduped):
        delta = len(deduped) - expected
        if delta < 0:
            duplicates_skipped += delta
        else:
            failed += delta
    return BulkWriteSummary(total, inserted, updated, duplicates_skipped, failed)
