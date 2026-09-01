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
        # Explicit timeouts prevent a hanging Python process if the MongoDB
        # host is unreachable; the defaults (30s connect, 30s server selection)
        # make interactive CI and local debugging painful with no upside.
        try:
            self._client = MongoClient(
                self.config.mongo_uri,
                connectTimeoutMS=MONGO_CONNECT_TIMEOUT_MS,
                serverSelectionTimeoutMS=MONGO_SERVER_SELECTION_TIMEOUT_MS,
            )
            # Force a round-trip so bad URIs or missing auth fail loudly here
            # instead of on the first write.
            self._client.admin.command("ping")
        except OperationFailure as exc:
            raise MongoConnectionError(
                f"MongoDB authentication/operation failed: {exc}"
            ) from exc
        except (ServerSelectionTimeoutError, NetworkTimeout, ConnectionFailure, ConfigurationError, AutoReconnect) as exc:
            # pymongo surfaces several distinct connection-failure exception
            # types; catching each explicitly avoids a broad except-Exception
            # that would swallow unrelated bugs (e.g. a typo in this method).
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
        # Unique index on url_hash is the dedup mechanism: bulk-writes that
        # would collide on URL raise DuplicateKeyError which we convert into
        # "skipped duplicate" accounting. A regular (non-unique) index on
        # scraped_at keeps recent-runs queries cheap.
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
        # Ordered=False keeps the bulk write going past individual duplicate
        # keys; DuplicateKeyError is raised after the bulk finishes with a
        # list of which indexes failed, so we can still count total successes.
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
        # Pre-Mongo dedup pass: scrapers for different boards occasionally
        # surface the same third-party job URL (e.g. a Greenhouse link shared
        # by both a WeWorkRemotely ad and a Remotive ad). Collapsing those in
        # Python before the bulk write avoids spurious DuplicateKeyError noise
        # in logs and keeps the per-run "new vs dup" counters honest.
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
