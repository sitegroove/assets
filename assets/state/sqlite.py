"""SQLiteBackend — state backend backed by a SQLite database."""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Generator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from assets.state.backend import StateBackend
from assets.state.db import connect_state
from assets.state.models import (
    AssetState,
    DependencyState,
    StateSnapshot,
)

logger = logging.getLogger(__name__)


def _parse_dt(s: str) -> datetime:
    """Parse ISO 8601 datetime string, tolerating several formats."""
    for fmt in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return datetime.fromisoformat(s)


def _asset_from_row(row: sqlite3.Row) -> AssetState:
    """Reconstruct an AssetState from a database row."""
    return AssetState(
        id=row["id"],
        type=row["type"],
        fingerprint=row["fingerprint"],
        data=json.loads(row["data"]),
        applied_at=_parse_dt(row["applied_at"]),
        applied_by=row["applied_by"],
        version=row["version"],
        deleted=bool(row["deleted"]),
    )


def _dep_from_row(row: sqlite3.Row) -> DependencyState:
    """Reconstruct a DependencyState from a database row."""
    return DependencyState(
        source=row["source"],
        target=row["target"],
        type=row["type"],
        fingerprint=row["fingerprint"],
        data=json.loads(row["data"]),
    )


class SQLiteBackend(StateBackend):
    """Stores state in a SQLite database with automatic version history.

    Layout:
        <db_path>   (single file, e.g. .assets_state/state.db)

    The database contains tables for environments, assets, dependencies,
    and append-only history tables populated via triggers.

    Pass ``":memory:"`` as *db_path* for a fast, transient in-memory
    database (useful for testing).  The in-memory backend exercises the
    same SQL schema, triggers, and serialization as the file-based one,
    giving full test parity.
    """

    def __init__(self, db_path: str | Path = ".assets_state/state.db") -> None:
        self._in_memory = str(db_path) == ":memory:"
        self._db_path: Path | None = None if self._in_memory else Path(db_path)
        self._conn: sqlite3.Connection | None = None
        self._locks: set[str] = set()

    @classmethod
    def memory(cls) -> SQLiteBackend:
        """Create a transient in-memory backend for testing.

        Equivalent to ``SQLiteBackend(":memory:")``.  The returned
        backend uses the same SQL schema and triggers as a file-backed
        instance, giving full test parity.
        """
        return cls(":memory:")

    @property
    def local_path(self) -> Path | None:
        """Directory containing the state database.

        Returns ``None`` for in-memory backends.
        """
        return self._db_path.parent if self._db_path is not None else None

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = connect_state(
                ":memory:" if self._in_memory else self._db_path  # type: ignore[arg-type]
            )
        return self._conn

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def load(self, environment: str) -> StateSnapshot | None:
        """Load full state for an environment. Returns None if not found."""
        row = self.conn.execute(
            "SELECT * FROM environments WHERE name = ?", (environment,)
        ).fetchone()
        if row is None:
            return None

        # Load assets
        asset_rows = self.conn.execute(
            "SELECT * FROM assets WHERE environment = ?", (environment,)
        ).fetchall()
        assets = {r["id"]: _asset_from_row(r) for r in asset_rows}

        # Load dependencies
        dep_rows = self.conn.execute(
            "SELECT * FROM dependencies WHERE environment = ?", (environment,)
        ).fetchall()
        deps = [_dep_from_row(r) for r in dep_rows]

        return StateSnapshot(
            version=row["version"],
            environment=row["name"],
            created_at=_parse_dt(row["created_at"]),
            updated_at=_parse_dt(row["updated_at"]),
            assets=assets,
            dependencies=deps,
            metadata=json.loads(row["metadata"]),
        )

    def save(
        self,
        environment: str,
        state: StateSnapshot,
        *,
        changed_ids: set[str] | None = None,
    ) -> None:
        """Save state for an environment.

        When *changed_ids* is ``None`` (default), every asset and
        dependency is upserted (full save).  When a set of asset IDs
        is provided, only those assets and their dependencies are
        written — drastically reducing SQLite I/O and avoiding
        spurious history-trigger rows for unchanged assets.
        """
        with self.conn:
            # Upsert environment
            self.conn.execute(
                """INSERT INTO environments
                       (name, version, created_at, updated_at, metadata)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(name) DO UPDATE SET
                       version = excluded.version,
                       updated_at = excluded.updated_at,
                       metadata = excluded.metadata""",
                (
                    environment,
                    state.version,
                    state.created_at.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
                    state.updated_at.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
                    json.dumps(state.metadata),
                ),
            )

            # Sync assets: delete removed, upsert current
            if changed_ids is not None:
                # Incremental: only delete assets in changed_ids that
                # are no longer in the desired snapshot.
                removed_ids = changed_ids - set(state.assets.keys())
            else:
                # Full save: compare all existing vs desired.
                existing_ids = {
                    r[0]
                    for r in self.conn.execute(
                        "SELECT id FROM assets WHERE environment = ?",
                        (environment,),
                    ).fetchall()
                }
                desired_ids = set(state.assets.keys())
                removed_ids = existing_ids - desired_ids

            if removed_ids:
                placeholders = ",".join("?" for _ in removed_ids)
                self.conn.execute(
                    (
                        "DELETE FROM assets "
                        f"WHERE environment = ? AND id IN ({placeholders})"
                    ),
                    (environment, *removed_ids),
                )

            # Determine which assets to upsert
            if changed_ids is not None:
                assets_to_write = (
                    state.assets[aid] for aid in changed_ids if aid in state.assets
                )
            else:
                assets_to_write = state.assets.values()

            # Batch upsert assets via executemany with generator
            # (generator avoids materializing all serialized rows in memory)
            self.conn.executemany(
                """INSERT INTO assets
                   (environment, id, type, fingerprint, data,
                    applied_at, applied_by, version, deleted)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(environment, id) DO UPDATE SET
                       type = excluded.type,
                       fingerprint = excluded.fingerprint,
                       data = excluded.data,
                       applied_at = excluded.applied_at,
                       applied_by = excluded.applied_by,
                       version = excluded.version,
                       deleted = excluded.deleted""",
                (
                    (
                        environment,
                        a.id,
                        a.type,
                        a.fingerprint,
                        json.dumps(a.data),
                        a.applied_at.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
                        a.applied_by,
                        a.version,
                        int(a.deleted),
                    )
                    for a in assets_to_write
                ),
            )

            # Incremental dependency persistence
            if changed_ids is not None:
                # Only rewrite deps touching changed assets
                dep_ids = changed_ids | removed_ids
                for aid in dep_ids:
                    self.conn.execute(
                        "DELETE FROM dependencies "
                        "WHERE environment = ? AND (source = ? OR target = ?)",
                        (environment, aid, aid),
                    )
                self.conn.executemany(
                    """INSERT OR REPLACE INTO dependencies
                       (environment, source, target, type, fingerprint, data)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (
                        (
                            environment,
                            dep.source,
                            dep.target,
                            dep.type,
                            dep.fingerprint,
                            json.dumps(dep.data),
                        )
                        for dep in state.dependencies
                        if dep.source in dep_ids or dep.target in dep_ids
                    ),
                )
            else:
                # Full dependency replace (original behaviour)
                self.conn.execute(
                    "DELETE FROM dependencies WHERE environment = ?",
                    (environment,),
                )
                self.conn.executemany(
                    """INSERT INTO dependencies
                       (environment, source, target, type, fingerprint, data)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (
                        (
                            environment,
                            dep.source,
                            dep.target,
                            dep.type,
                            dep.fingerprint,
                            json.dumps(dep.data),
                        )
                        for dep in state.dependencies
                    ),
                )

    @contextmanager
    def lock(self, environment: str) -> Generator[None, None, None]:
        """In-process lock for single-process use."""
        if environment in self._locks:
            raise RuntimeError(f"Environment '{environment}' is already locked")
        self._locks.add(environment)
        try:
            yield
        finally:
            self._locks.discard(environment)

    def list_environments(self) -> list[str]:
        rows = self.conn.execute(
            "SELECT name FROM environments ORDER BY name"
        ).fetchall()
        return [r["name"] for r in rows]

    def delete_environment(self, environment: str) -> None:
        with self.conn:
            self.conn.execute(
                "DELETE FROM dependencies WHERE environment = ?", (environment,)
            )
            self.conn.execute(
                "DELETE FROM assets WHERE environment = ?", (environment,)
            )
            # History tables also have FK to environments
            self.conn.execute(
                "DELETE FROM assets_history WHERE environment = ?", (environment,)
            )
            self.conn.execute(
                "DELETE FROM dependencies_history WHERE environment = ?", (environment,)
            )
            self.conn.execute("DELETE FROM environments WHERE name = ?", (environment,))

    # ─── History queries ────────────────────────────────────

    def asset_history(
        self,
        environment: str,
        asset_id: str,
        *,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Return version history for a specific asset."""
        rows = self.conn.execute(
            """SELECT id, environment, asset_id, action, type, fingerprint, data,
                      applied_at, applied_by, version, recorded_at
               FROM assets_history
               WHERE environment = ? AND asset_id = ?
               ORDER BY version DESC
               LIMIT ?""",
            (environment, asset_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def environment_changelog(
        self,
        environment: str,
        *,
        since: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Return recent changes across all assets in an environment."""
        if since:
            rows = self.conn.execute(
                """SELECT id, asset_id, action, applied_by, applied_at,
                          version, recorded_at
                   FROM assets_history
                   WHERE environment = ? AND recorded_at > ?
                   ORDER BY recorded_at DESC
                   LIMIT ?""",
                (environment, since, limit),
            ).fetchall()
        else:
            rows = self.conn.execute(
                """SELECT id, asset_id, action, applied_by, applied_at,
                          version, recorded_at
                   FROM assets_history
                   WHERE environment = ?
                   ORDER BY recorded_at DESC
                   LIMIT ?""",
                (environment, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def asset_version(
        self, environment: str, asset_id: str, version: int
    ) -> dict[str, Any] | None:
        """Retrieve a specific historical version of an asset."""
        row = self.conn.execute(
            """SELECT * FROM assets_history
               WHERE environment = ? AND asset_id = ? AND version = ?""",
            (environment, asset_id, version),
        ).fetchone()
        return dict(row) if row else None

    def prune_history(self, *, keep_days: int = 90) -> int:
        """Delete history entries older than keep_days. Returns count removed."""
        cur = self.conn.execute(
            """DELETE FROM assets_history
               WHERE recorded_at < strftime('%Y-%m-%dT%H:%M:%fZ', 'now', ?)""",
            (f"-{keep_days} days",),
        )
        dep_cur = self.conn.execute(
            """DELETE FROM dependencies_history
               WHERE recorded_at < strftime('%Y-%m-%dT%H:%M:%fZ', 'now', ?)""",
            (f"-{keep_days} days",),
        )
        total = (cur.rowcount or 0) + (dep_cur.rowcount or 0)
        self.conn.commit()
        return total
