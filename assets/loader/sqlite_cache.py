"""SQLiteCompiledCache — compiled cache backed by SQLite."""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from pathlib import Path
from typing import Any

from assets.state.db import connect_compiled

logger = logging.getLogger(__name__)


class SQLiteCompiledCache:
    """Per-file persistent cache using SQLite instead of individual JSON files.

    Drop-in replacement for CompiledCache with the same get/put/clean interface.
    Stores all entries in a single .assets_state/compiled.db file.
    """

    def __init__(self, db_path: str | Path = ".assets_state/compiled.db") -> None:
        self._db_path = Path(db_path)
        self._conn: sqlite3.Connection | None = None

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = connect_compiled(self._db_path)
        return self._conn

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    @staticmethod
    def _content_hash(file_path: Path) -> str:
        return hashlib.sha256(file_path.read_bytes()).hexdigest()

    @staticmethod
    def _mtime_ns(file_path: Path) -> int:
        return file_path.stat().st_mtime_ns

    @staticmethod
    def _rel_path(source_path: Path, root: Path) -> str:
        """Compute a relative path key for the cache."""
        try:
            return str(source_path.resolve().relative_to(root.resolve()))
        except ValueError:
            return source_path.name

    def get(self, source_path: Path, root: Path) -> dict[str, Any] | None:
        """Return cached asset dict if fresh, None if stale or missing."""
        key = self._rel_path(source_path, root)
        row = self.conn.execute(
            "SELECT source_mtime, content_hash, data FROM compiled WHERE path = ?",
            (key,),
        ).fetchone()
        if row is None:
            return None

        # Fast path: mtime match
        try:
            current_mtime = self._mtime_ns(source_path)
        except OSError:
            return None

        if row["source_mtime"] == current_mtime:
            try:
                return json.loads(row["data"])
            except (json.JSONDecodeError, TypeError):
                self.conn.execute("DELETE FROM compiled WHERE path = ?", (key,))
                self.conn.commit()
                return None

        # Slow path: content hash (handles git checkout, CI clone)
        current_hash = self._content_hash(source_path)
        if row["content_hash"] == current_hash:
            # Update mtime for next fast-path hit
            self.conn.execute(
                "UPDATE compiled SET source_mtime = ? WHERE path = ?",
                (current_mtime, key),
            )
            self.conn.commit()
            try:
                return json.loads(row["data"])
            except (json.JSONDecodeError, TypeError):
                self.conn.execute("DELETE FROM compiled WHERE path = ?", (key,))
                self.conn.commit()
                return None

        # Real change — cache miss
        return None

    def put(self, source_path: Path, root: Path, data: dict[str, Any]) -> None:
        """Store compiled asset dict."""
        key = self._rel_path(source_path, root)
        self.conn.execute(
            """INSERT INTO compiled (path, source_mtime, content_hash, data)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(path) DO UPDATE SET
                   source_mtime = excluded.source_mtime,
                   content_hash = excluded.content_hash,
                   data = excluded.data""",
            (
                key,
                self._mtime_ns(source_path),
                self._content_hash(source_path),
                json.dumps(data),
            ),
        )
        self.conn.commit()

    def put_many(
        self, entries: list[tuple[Path, Path, dict[str, Any]]]
    ) -> None:
        """Batch-insert multiple compiled entries in a single transaction.

        Uses a generator so only one serialized row is in memory at a time,
        avoiding large allocations when assets carry big data blobs.

        Args:
            entries: list of (source_path, root, data) tuples.
        """
        with self.conn:
            self.conn.executemany(
                """INSERT INTO compiled (path, source_mtime, content_hash, data)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(path) DO UPDATE SET
                       source_mtime = excluded.source_mtime,
                       content_hash = excluded.content_hash,
                       data = excluded.data""",
                (
                    (
                        self._rel_path(src, root),
                        self._mtime_ns(src),
                        self._content_hash(src),
                        json.dumps(data),
                    )
                    for src, root, data in entries
                ),
            )

    def clean(self, root: Path) -> int:
        """Remove entries whose source files no longer exist. Returns count removed."""
        rows = self.conn.execute("SELECT path FROM compiled").fetchall()
        removed = 0
        to_delete: list[str] = []
        for row in rows:
            rel = row["path"]
            found = False
            for ext in (".yaml", ".yml", ".py", ".sql", ".json"):
                candidate = root / Path(rel).with_suffix(ext)
                if candidate.exists():
                    found = True
                    break
            # Also check the path as-is (exact match)
            if not found and (root / rel).exists():
                found = True
            if not found:
                to_delete.append(rel)
                removed += 1

        if to_delete:
            placeholders = ",".join("?" for _ in to_delete)
            self.conn.execute(
                f"DELETE FROM compiled WHERE path IN ({placeholders})",
                to_delete,
            )
            self.conn.commit()
        return removed
