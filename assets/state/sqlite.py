"""SQLiteBackend — state backend backed by a SQLite database."""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Generator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from assets.state.backend import StateBackend
from assets.state.db import connect_state
from assets.state.models import (
    AssetState,
    DependencyState,
    SourceFileRef,
    StateSnapshot,
)

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _parse_dt(s: str) -> datetime:
    """Parse ISO 8601 datetime string, tolerating several formats."""
    for fmt in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=UTC)
        except ValueError:
            continue
    return datetime.fromisoformat(s)


def _asset_from_row(row: sqlite3.Row) -> AssetState:
    """Reconstruct an AssetState from a database row."""
    return AssetState(
        name=row["name"],
        kind=row["kind"],
        fingerprint=row["fingerprint"],
        data=json.loads(row["data"]),
        source_files=[SourceFileRef.model_validate(sf) for sf in json.loads(row["source_files"])],
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
    """

    def __init__(self, db_path: str | Path = ".assets_state/state.db") -> None:
        self._db_path = Path(db_path)
        self._conn: sqlite3.Connection | None = None
        self._locks: set[str] = set()

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = connect_state(self._db_path)
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
        assets = {r["name"]: _asset_from_row(r) for r in asset_rows}

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

    def save(self, environment: str, state: StateSnapshot) -> None:
        """Save state for an environment. Upserts everything in a transaction."""
        with self.conn:
            # Upsert environment
            self.conn.execute(
                """INSERT INTO environments (name, version, created_at, updated_at, metadata)
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
            existing_names = {
                r[0]
                for r in self.conn.execute(
                    "SELECT name FROM assets WHERE environment = ?", (environment,)
                ).fetchall()
            }
            desired_names = set(state.assets.keys())
            removed_names = existing_names - desired_names

            if removed_names:
                placeholders = ",".join("?" for _ in removed_names)
                self.conn.execute(
                    f"DELETE FROM assets WHERE environment = ? AND name IN ({placeholders})",
                    (environment, *removed_names),
                )

            # Batch upsert assets via executemany with generator
            # (generator avoids materializing all serialized rows in memory)
            self.conn.executemany(
                """INSERT INTO assets
                   (environment, name, kind, fingerprint, data, source_files,
                    applied_at, applied_by, version, deleted)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(environment, name) DO UPDATE SET
                       kind = excluded.kind,
                       fingerprint = excluded.fingerprint,
                       data = excluded.data,
                       source_files = excluded.source_files,
                       applied_at = excluded.applied_at,
                       applied_by = excluded.applied_by,
                       version = excluded.version,
                       deleted = excluded.deleted""",
                (
                    (
                        environment,
                        a.name,
                        a.kind,
                        a.fingerprint,
                        json.dumps(a.data),
                        json.dumps([sf.model_dump() for sf in a.source_files]),
                        a.applied_at.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
                        a.applied_by,
                        a.version,
                        int(a.deleted),
                    )
                    for a in state.assets.values()
                ),
            )

            # Batch insert dependencies via executemany with generator
            self.conn.execute(
                "DELETE FROM dependencies WHERE environment = ?", (environment,)
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
        """In-process lock (same as MemoryBackend for single-process use)."""
        if environment in self._locks:
            raise RuntimeError(f"Environment '{environment}' is already locked")
        self._locks.add(environment)
        try:
            yield
        finally:
            self._locks.discard(environment)

    def list_environments(self) -> list[str]:
        rows = self.conn.execute("SELECT name FROM environments ORDER BY name").fetchall()
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
            self.conn.execute(
                "DELETE FROM environments WHERE name = ?", (environment,)
            )

    # ─── History queries ────────────────────────────────────

    def asset_history(
        self,
        environment: str,
        name: str,
        *,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Return version history for a specific asset."""
        rows = self.conn.execute(
            """SELECT id, environment, name, action, kind, fingerprint, data,
                      source_files, applied_at, applied_by, version, recorded_at
               FROM assets_history
               WHERE environment = ? AND name = ?
               ORDER BY version DESC
               LIMIT ?""",
            (environment, name, limit),
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
                """SELECT id, name, action, applied_by, applied_at, version, recorded_at
                   FROM assets_history
                   WHERE environment = ? AND recorded_at > ?
                   ORDER BY recorded_at DESC
                   LIMIT ?""",
                (environment, since, limit),
            ).fetchall()
        else:
            rows = self.conn.execute(
                """SELECT id, name, action, applied_by, applied_at, version, recorded_at
                   FROM assets_history
                   WHERE environment = ?
                   ORDER BY recorded_at DESC
                   LIMIT ?""",
                (environment, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def asset_version(
        self, environment: str, name: str, version: int
    ) -> dict[str, Any] | None:
        """Retrieve a specific historical version of an asset."""
        row = self.conn.execute(
            """SELECT * FROM assets_history
               WHERE environment = ? AND name = ? AND version = ?""",
            (environment, name, version),
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
