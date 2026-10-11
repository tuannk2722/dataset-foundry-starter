"""Persistent exact-content index shared by multiple collection runs."""

from __future__ import annotations

import hashlib
import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Self

SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
SCHEMA_VERSION = 1


@dataclass(frozen=True)
class HashRecord:
    sha256: str
    bytes: int
    file_path: Path
    run_id: str
    asset_id: str
    indexed_at: str


def _validate_digest(sha256: str) -> str:
    if not isinstance(sha256, str) or not SHA256_RE.fullmatch(sha256):
        raise ValueError("sha256 must be 64 lowercase hexadecimal characters")
    return sha256


def _file_digest(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(64 * 1024), b""):
            size += len(chunk)
            digest.update(chunk)
    return size, digest.hexdigest()


class HashIndex:
    """SQLite-backed SHA-256 index with validation of referenced raw assets."""

    def __init__(self, path: str | Path):
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._connection: sqlite3.Connection | None = sqlite3.connect(self.path, timeout=30)
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA foreign_keys=ON")
        self._initialize()

    def _require_connection(self) -> sqlite3.Connection:
        if self._connection is None:
            raise RuntimeError("hash index is closed")
        return self._connection

    def _initialize(self) -> None:
        connection = self._require_connection()
        with connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            row = connection.execute(
                "SELECT value FROM metadata WHERE key = 'schema_version'"
            ).fetchone()
            if row is not None and row[0] != str(SCHEMA_VERSION):
                raise ValueError(f"unsupported hash index schema version: {row[0]}")
            connection.execute(
                "INSERT OR IGNORE INTO metadata(key, value) VALUES ('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS assets (
                    sha256 TEXT PRIMARY KEY,
                    bytes INTEGER NOT NULL CHECK(bytes >= 0),
                    file_path TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    asset_id TEXT NOT NULL,
                    indexed_at TEXT NOT NULL
                )
                """
            )

    @staticmethod
    def _record(row: tuple[object, ...]) -> HashRecord:
        return HashRecord(
            sha256=str(row[0]),
            bytes=int(row[1]),
            file_path=Path(str(row[2])),
            run_id=str(row[3]),
            asset_id=str(row[4]),
            indexed_at=str(row[5]),
        )

    @staticmethod
    def _target_is_valid(record: HashRecord) -> bool:
        try:
            if not record.file_path.is_file() or record.file_path.stat().st_size != record.bytes:
                return False
            size, digest = _file_digest(record.file_path)
        except OSError:
            return False
        return size == record.bytes and digest == record.sha256

    def lookup(self, sha256: str, *, expected_bytes: int | None = None) -> HashRecord | None:
        """Return a verified record, deleting a stale target before returning ``None``."""
        digest = _validate_digest(sha256)
        if expected_bytes is not None and (
            not isinstance(expected_bytes, int)
            or isinstance(expected_bytes, bool)
            or expected_bytes < 0
        ):
            raise ValueError("expected_bytes must be a non-negative integer")
        connection = self._require_connection()
        row = connection.execute(
            "SELECT sha256, bytes, file_path, run_id, asset_id, indexed_at "
            "FROM assets WHERE sha256 = ?",
            (digest,),
        ).fetchone()
        if row is None:
            return None
        record = self._record(row)
        if not self._target_is_valid(record):
            with connection:
                connection.execute(
                    "DELETE FROM assets WHERE sha256 = ? AND file_path = ?",
                    (record.sha256, str(record.file_path)),
                )
            return None
        if expected_bytes is not None and record.bytes != expected_bytes:
            return None
        return record

    def register(
        self,
        sha256: str,
        byte_count: int,
        file_path: str | Path,
        *,
        run_id: str,
        asset_id: str,
    ) -> HashRecord:
        """Register a verified file, preserving an existing valid first occurrence."""
        digest = _validate_digest(sha256)
        if (
            not isinstance(byte_count, int)
            or isinstance(byte_count, bool)
            or byte_count < 0
        ):
            raise ValueError("byte_count must be a non-negative integer")
        if not isinstance(run_id, str) or not run_id.strip():
            raise ValueError("run_id must be a non-empty string")
        if not isinstance(asset_id, str) or not asset_id.strip():
            raise ValueError("asset_id must be a non-empty string")
        target = Path(file_path).resolve()
        if not target.is_file():
            raise FileNotFoundError(target)
        actual_size, actual_digest = _file_digest(target)
        if actual_size != byte_count or actual_digest != digest:
            raise ValueError("indexed file does not match supplied bytes and sha256")

        existing = self.lookup(digest, expected_bytes=byte_count)
        if existing is not None:
            return existing

        indexed_at = datetime.now(UTC).isoformat()
        connection = self._require_connection()
        with connection:
            connection.execute(
                """
                INSERT INTO assets(sha256, bytes, file_path, run_id, asset_id, indexed_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(sha256) DO NOTHING
                """,
                (digest, byte_count, str(target), run_id, asset_id, indexed_at),
            )
        record = self.lookup(digest, expected_bytes=byte_count)
        if record is None:  # The file changed between validation and commit.
            raise ValueError("indexed file changed during registration")
        return record

    def close(self) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None

    def __enter__(self) -> Self:
        self._require_connection()
        return self

    def __exit__(self, _exc_type, _exc, _traceback) -> None:
        self.close()
