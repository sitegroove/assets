"""SQLiteBackend — state backend with one SQLite database per environment.

Layout (file-backed)::

    <base_path>/
    ├── default/
    │   └── state.db
    ├── production/
    │   └── state.db
    └── staging/
        └── state.db

Pass ``":memory:"`` as *base_path* for fast, transient in-memory
databases (useful for testing).  Each environment gets its own
``:memory:`` connection, exercising the same SQL schema, triggers,
and serialization as file-backed instances.
"""

from __future__ import annotations

import json
import logging
import shutil
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
    """Stores state in per-environment SQLite databases.

    Each environment gets its own ``state.db`` in a subdirectory
    of *base_path*, providing physical isolation so that concurrent
    writes to different environments never conflict.

    Pass ``":memory:"`` as *base_path* for fast, transient in-memory
    databases (useful for testing).  The in-memory backend exercises
    the same SQL schema, triggers, and serialization as the
    file-based one, giving full test parity.
    """

    def __init__(self, base_path: str | Path = ".assets_state") -> None:
        self._in_memory = str(base_path) == ":memory:"
        self._base_path: Path | None = None if self._in_memory else Path(base_path)
        self._connections: dict[str, sqlite3.Connection] = {}
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
        """Base directory containing per-environment state databases.

        Returns ``None`` for in-memory backends.
        """
        return self._base_path

    def env_db_path(self, environment: str) -> Path:
        """Path to the state.db for a specific environment.

        Raises ``RuntimeError`` for in-memory backends.
        """
        if self._base_path is None:
            raise RuntimeError("In-memory backends do not have a filesystem path.")
        return self._base_path / environment / "state.db"

    def conn(self, environment: str) -> sqlite3.Connection:
        """Get or create the SQLite connection for an environment."""
        if environment not in self._connections:
            if self._in_memory:
                self._connections[environment] = connect_state(":memory:")
            else:
                self._connections[environment] = connect_state(
                    self.env_db_path(environment),
                )
        return self._connections[environment]

    def close_env(self, environment: str) -> None:
        """Close the connection for a specific environment."""
        conn = self._connections.pop(environment, None)
        if conn is not None:
            conn.close()

    def close(self) -> None:
        """Close all environment connections."""
        for conn in self._connections.values():
            conn.close()
        self._connections.clear()

    def load(self, environment: str) -> StateSnapshot | None:
        """Load full state for an environment. Returns None if not found."""
        c = self.conn(environment)

        row = c.execute("SELECT * FROM state_metadata").fetchone()
        if row is None:
            return None

        # Load assets
        asset_rows = c.execute("SELECT * FROM assets").fetchall()
        assets = {r["id"]: _asset_from_row(r) for r in asset_rows}

        # Load dependencies
        dep_rows = c.execute("SELECT * FROM dependencies").fetchall()
        deps = [_dep_from_row(r) for r in dep_rows]

        return StateSnapshot(
            version=row["version"],
            environment=environment,
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
        c = self.conn(environment)
        with c:
            # Upsert state metadata (single row)
            c.execute(
                """INSERT INTO state_metadata
                       (version, created_at, updated_at, metadata)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(rowid) DO UPDATE SET
                       version = excluded.version,
                       updated_at = excluded.updated_at,
                       metadata = excluded.metadata""",
                (
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
                    r[0] for r in c.execute("SELECT id FROM assets").fetchall()
                }
                desired_ids = set(state.assets.keys())
                removed_ids = existing_ids - desired_ids

            if removed_ids:
                placeholders = ",".join("?" for _ in removed_ids)
                c.execute(
                    f"DELETE FROM assets WHERE id IN ({placeholders})",
                    tuple(removed_ids),
                )

            # Determine which assets to upsert
            if changed_ids is not None:
                assets_to_write = (
                    state.assets[aid] for aid in changed_ids if aid in state.assets
                )
            else:
                assets_to_write = state.assets.values()

            # Batch upsert assets via executemany with generator
            c.executemany(
                """INSERT INTO assets
                   (id, type, fingerprint, data,
                    applied_at, applied_by, version)
                   VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET
                       type = excluded.type,
                       fingerprint = excluded.fingerprint,
                       data = excluded.data,
                       applied_at = excluded.applied_at,
                       applied_by = excluded.applied_by,
                       version = excluded.version""",
                (
                    (
                        a.id,
                        a.type,
                        a.fingerprint,
                        json.dumps(a.data),
                        a.applied_at.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
                        a.applied_by,
                        a.version,
                    )
                    for a in assets_to_write
                ),
            )

            # Incremental dependency persistence
            if changed_ids is not None:
                # Only rewrite deps touching changed assets
                dep_ids = changed_ids | removed_ids
                for aid in dep_ids:
                    c.execute(
                        "DELETE FROM dependencies WHERE source = ? OR target = ?",
                        (aid, aid),
                    )
                c.executemany(
                    """INSERT OR REPLACE INTO dependencies
                       (source, target, type, fingerprint, data)
                       VALUES (?, ?, ?, ?, ?)""",
                    (
                        (
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
                c.execute("DELETE FROM dependencies")
                c.executemany(
                    """INSERT INTO dependencies
                       (source, target, type, fingerprint, data)
                       VALUES (?, ?, ?, ?, ?)""",
                    (
                        (
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
        """List environments that have stored state.

        For file-backed backends, scans subdirectories containing
        ``state.db``.  For in-memory backends, returns environment
        names that have open connections with data.
        """
        if self._in_memory:
            envs: list[str] = []
            for env_name, conn in self._connections.items():
                row = conn.execute("SELECT * FROM state_metadata").fetchone()
                if row is not None:
                    envs.append(env_name)
            return sorted(envs)

        if self._base_path is None or not self._base_path.exists():
            return []
        envs = []
        for entry in sorted(self._base_path.iterdir()):
            if entry.is_dir() and (entry / "state.db").exists():
                envs.append(entry.name)
        return envs

    def delete_environment(self, environment: str) -> None:
        """Delete all state for an environment.

        For file-backed backends, removes the environment directory.
        For in-memory backends, closes and discards the connection.
        """
        self.close_env(environment)
        if not self._in_memory and self._base_path is not None:
            env_dir = self._base_path / environment
            if env_dir.exists():
                shutil.rmtree(env_dir)

    def copy_environment(self, source: str, target: str) -> None:
        """Copy state from one environment to another.

        For file-backed backends, copies the ``state.db`` file
        directly (after flushing the WAL) for maximum efficiency.
        For in-memory backends, falls back to load-then-save.
        """
        if self._in_memory:
            super().copy_environment(source, target)
            return

        src_path = self.env_db_path(source)
        if not src_path.exists():
            return

        # Flush WAL on source to make the DB file self-contained
        if source in self._connections:
            self._connections[source].execute("PRAGMA wal_checkpoint(TRUNCATE)")

        dst_path = self.env_db_path(target)
        dst_path.parent.mkdir(parents=True, exist_ok=True)

        # Close target connection if open (we're overwriting it)
        self.close_env(target)
        shutil.copy2(str(src_path), str(dst_path))

    # ─── History queries ────────────────────────────────────

    def asset_history(
        self,
        environment: str,
        asset_id: str,
        *,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Return version history for a specific asset."""
        c = self.conn(environment)
        rows = c.execute(
            """SELECT id, asset_id, action, type, fingerprint, data,
                      applied_at, applied_by, version, recorded_at
               FROM assets_history
               WHERE asset_id = ?
               ORDER BY version DESC
               LIMIT ?""",
            (asset_id, limit),
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
        c = self.conn(environment)
        if since:
            rows = c.execute(
                """SELECT id, asset_id, action, applied_by, applied_at,
                          version, recorded_at
                   FROM assets_history
                   WHERE recorded_at > ?
                   ORDER BY recorded_at DESC
                   LIMIT ?""",
                (since, limit),
            ).fetchall()
        else:
            rows = c.execute(
                """SELECT id, asset_id, action, applied_by, applied_at,
                          version, recorded_at
                   FROM assets_history
                   ORDER BY recorded_at DESC
                   LIMIT ?""",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    def asset_version(
        self, environment: str, asset_id: str, version: int
    ) -> dict[str, Any] | None:
        """Retrieve a specific historical version of an asset."""
        c = self.conn(environment)
        row = c.execute(
            """SELECT * FROM assets_history
               WHERE asset_id = ? AND version = ?""",
            (asset_id, version),
        ).fetchone()
        return dict(row) if row else None

    def prune_history(self, *, keep_days: int = 90) -> int:
        """Delete history entries older than keep_days across all envs.

        Returns total count of removed rows.
        """
        total = 0
        for env_name in self.list_environments():
            c = self.conn(env_name)
            cur = c.execute(
                """DELETE FROM assets_history
                   WHERE recorded_at < strftime('%Y-%m-%dT%H:%M:%fZ', 'now', ?)""",
                (f"-{keep_days} days",),
            )
            dep_cur = c.execute(
                """DELETE FROM dependencies_history
                   WHERE recorded_at < strftime('%Y-%m-%dT%H:%M:%fZ', 'now', ?)""",
                (f"-{keep_days} days",),
            )
            total += (cur.rowcount or 0) + (dep_cur.rowcount or 0)
            c.commit()
        return total
