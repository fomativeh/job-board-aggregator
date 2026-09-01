from __future__ import annotations

import logging
from typing import Any, Iterable, Optional, Sequence

from pymongo import MongoClient
from pymongo.collection import Collection
from pymongo.database import Database
from pymongo.errors import (
    AutoReconnect,
    ConfigurationError,
    ConnectionFailure,
    DuplicateKeyError,
    NetworkTimeout,
    OperationFailure,
    ServerSelectionTimeoutError,
)

from .config import Config
from .schema import JobListing, make_url_hash, validate_listing

log: logging.Logger = logging.getLogger(__name__)

URL_HASH_FIELD: str = "url_hash"

MONGO_CONNECT_TIMEOUT_MS: int = 10_000
MONGO_SERVER_SELECTION_TIMEOUT_MS: int = 10_000


class MongoConnectionError(Exception):
    pass


class Storage:
    def __init__(self, config: Config) -> None:
        self.config: Config = config
        self._client: Optional[MongoClient[Any]] = None
        self._collection: Optional[Collection[Any]] = None

    def connect(self) -> None:
        try:
            self._client = MongoClient(
                self.config.mongo_uri,
                connectTimeoutMS=MONGO_CONNECT_TIMEOUT_MS,
                serverSelectionTimeoutMS=MONGO_SERVER_SELECTION_TIMEOUT_MS,
            )
            self._client.admin.command("ping")
        except OperationFailure as exc:
            raise MongoConnectionError(
                f"MongoDB authentication/operation failed: {exc}"
            ) from exc
        except (ServerSelectionTimeoutError, NetworkTimeout, ConnectionFailure, ConfigurationError, AutoReconnect) as exc:
            raise MongoConnectionError(
                f"Could not connect to MongoDB at {self.config.mongo_uri!r}: {exc}"
            ) from exc

        db: Database[Any] = self._client[self.config.mongo_db]
        self._collection = db[self.config.mongo_collection]
        self._ensure_indexes()
        log.info(
            "Connected to MongoDB db=%s collection=%s",
            self.config.mongo_db,
            self.config.mongo_collection,
        )

    def _ensure_indexes(self) -> None:
        assert self._collection is not None
        self._collection.create_index(URL_HASH_FIELD, unique=True)
        self._collection.create_index("scraped_at")
        self._collection.create_index("source")

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None
            self._collection = None
            log.info("MongoDB connection closed")

    def _get_collection(self) -> Collection[Any]:
        if self._collection is None:
            raise MongoConnectionError(
                "Storage is not connected; call connect() before operations"
            )
        return self._collection

    @staticmethod
    def _add_hash(listing: JobListing) -> dict[str, object]:
        doc: dict[str, object] = dict(listing)
        doc[URL_HASH_FIELD] = make_url_hash(listing["url"])
        return doc

    def insert_many_unique(
        self, listings: Sequence[JobListing]
    ) -> tuple[int, int]:
        if not listings:
            log.info("No listings provided to insert_many_unique — skipping")
            return 0, 0

        for listing in listings:
            validate_listing(listing)

        collection = self._get_collection()
        docs: list[dict[str, object]] = [self._add_hash(l) for l in listings]

        inserted: int = 0
        duplicates: int = 0
        try:
            result = collection.insert_many(docs, ordered=False)
            inserted = len(result.inserted_ids)
        except DuplicateKeyError as exc:
            write_errors: list[dict[str, Any]] = exc.details.get("writeErrors", []) if exc.details else []
            failed_indexes: set[int] = {err["index"] for err in write_errors}
            inserted = len(docs) - len(failed_indexes)
            duplicates = len(failed_indexes)
            log.warning(
                "Bulk insert hit %d duplicate URL hashes; %d new listings inserted",
                duplicates,
                inserted,
            )

        log.info(
            "insert_many_unique complete: inserted=%d duplicates_skipped=%d total_in=%d",
            inserted,
            duplicates,
            len(listings),
        )
        return inserted, duplicates

    def dedup_in_memory(
        self, listings: Iterable[JobListing]
    ) -> list[JobListing]:
        seen_hashes: set[str] = set()
        deduped: list[JobListing] = []
        dropped = 0
        for listing in listings:
            url_hash = make_url_hash(listing["url"])
            if url_hash in seen_hashes:
                dropped += 1
                continue
            seen_hashes.add(url_hash)
            deduped.append(listing)
        if dropped:
            log.warning("Pre-Mongo dedup dropped %d cross-source URL duplicates", dropped)
        return deduped
