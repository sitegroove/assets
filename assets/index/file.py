"""FileIndex — file-based freshness tracker backed by SQLite.

Tracks source files and their dependencies on other files (companion
SQL, macros, vars).  Uses a two-tier freshness check (mtime fast
path -> content hash fallback) for both source files and their
dependencies.

The index does **not** store compiled asset data — that lives in
the state backend's ``assets`` table.  The index only answers
"has this file changed since last indexing?"

Index tables (``index_entries``, ``index_deps``) live in a
**separate** ``index.db`` database that is never synced to remote
storage.  This avoids pushing machine-local file metadata to
shared state.

Entries are scoped by a logical *group* name so that multiple
independent source roots (e.g. modules installed in different
locations) can coexist without collision.

**Mtime tracking** is stored in a local JSON file (never synced
to remote) so that mtime-only changes (save/revert, git checkout)
do not dirty the index database.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import threading
from dataclasses import dataclass, field
from pathlib import Path

from assets.index.base import Index
from assets.state.db import connect_index

logger = logging.getLogger(__name__)

_SOURCE_EXTENSIONS = (".yaml", ".yml", ".py", ".sql", ".json")


@dataclass
class IndexStatus:
    """Result of classifying source files against the index."""

    fresh: list[tuple[Path, str]] = field(default_factory=list)
    """Fresh entries as ``(path, asset_id)`` pairs."""

    changed: list[Path] = field(default_factory=list)
    new: list[Path] = field(default_factory=list)

    deleted: list[tuple[str, str]] = field(default_factory=list)
    """Deleted entries as ``(location, asset_id)`` pairs."""

    @property
    def stale(self) -> list[Path]:
        """All files needing (re-)parsing: changed + new."""
        return self.changed + self.new

    @property
    def fresh_ids(self) -> list[str]:
        """Asset ids of all fresh entries."""
        return [asset_id for _, asset_id in self.fresh]


class MtimeCache:
    """Local-only mtime cache backed by a JSON file.

    Maps ``key → mtime_ns`` for both source files and deps.
    Keys are namespaced by group (``group:location``) to avoid
    collisions between groups.  This file is never pushed to remote
    storage — it exists only to provide the mtime fast-path
    optimization so that hash-identical-but-mtime-different files
    (save/revert, git checkout) skip hashing on the next run.

    When ``path`` is ``None``, the cache is in-memory only (useful
    for tests).

    Thread-safe: all reads/writes are guarded by a lock.
    """

    def __init__(self, path: Path | None = None) -> None:
        self._lock = threading.Lock()
        self._path = path
        self._data: dict[str, int] = {}
        if path is not None and path.exists():
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    self._data = {k: int(v) for k, v in raw.items()}
            except (json.JSONDecodeError, ValueError, OSError):
                logger.debug(
                    "Could not load mtime cache at %s, starting fresh",
                    path,
                )

    def get(self, key: str) -> int | None:
        """Return cached mtime_ns or ``None`` if not cached."""
        with self._lock:
            return self._data.get(key)

    def set(self, key: str, mtime_ns: int) -> None:
        """Update the cached mtime for a key."""
        with self._lock:
            self._data[key] = mtime_ns

    def discard(self, key: str) -> None:
        """Remove a key from the cache (no error if missing)."""
        with self._lock:
            self._data.pop(key, None)

    def flush(self) -> None:
        """Persist the cache to disk (no-op if in-memory only)."""
        with self._lock:
            if self._path is None:
                return
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(
                json.dumps(self._data, separators=(",", ":")),
                encoding="utf-8",
            )


class FileIndex(Index):
    """File-based freshness tracker backed by SQLite.

    Each entry maps a source file *location* (relative path) within
    a logical *group* to the asset it produces.  Dependencies on
    other files (companion SQL, Jinja macros, variable files) are
    tracked per-entry so that a change to any dependency marks only
    the affected entries as stale.

    The *group* allows multiple independent source roots (e.g.
    modules installed at different filesystem locations) to coexist
    in the same index without collision.  Two entries may share the
    same *location* as long as they belong to different groups.

    Compiled asset data is **not** stored here — consumers retrieve
    fresh assets from the state backend's ``assets`` table.

    Mtime values are stored in a **local JSON cache** (never synced)
    so that mtime-only changes don't dirty the shared ``state.db``.

    Two-tier freshness for both source files and dependencies:

    1. **mtime match** (from local cache) -- instant hit (~0.1 ms).
    2. **content-hash match** -- handles ``git checkout`` / CI clone.
       Updates local mtime cache so the next check takes the fast path.
    3. **Neither** -- real change, entry is stale.

    Consumers should not construct this directly — use
    :attr:`StateManager.index` instead::

        manager = StateManager.create(registry, local_path=".assets_state")
        index = manager.index
    """

    def __init__(
        self,
        index_db_path: str | Path,
        cache_dir: Path,
    ) -> None:
        self._conn = connect_index(index_db_path)
        self._mtime_cache = MtimeCache(cache_dir / "mtime_cache.json")

    @property
    def conn(self) -> sqlite3.Connection:
        return self._conn

    def close(self) -> None:
        """Close the index DB connection and flush the mtime cache."""
        self._mtime_cache.flush()
        self._conn.close()

    # ── helpers ──────────────────────────────────────────────

    @staticmethod
    def _content_hash(file_path: Path) -> str:
        """SHA-256 hash of file contents."""
        return hashlib.sha256(file_path.read_bytes()).hexdigest()

    @staticmethod
    def _mtime_ns(file_path: Path) -> int:
        """Nanosecond-precision modification time."""
        return file_path.stat().st_mtime_ns

    @staticmethod
    def _rel_path(source_path: Path, root: Path) -> str:
        """Compute a relative path key for the index."""
        try:
            return str(source_path.resolve().relative_to(root.resolve()))
        except ValueError:
            return source_path.name

    @staticmethod
    def _cache_key(group: str, location: str) -> str:
        """Build a namespaced key for the mtime cache."""
        if group:
            return f"{group}:{location}"
        return location

    # ── Index ABC implementation ─────────────────────────────

    def put(
        self,
        location: str,
        *,
        group: str = "",
        asset_id: str,
        fingerprint: str,
        deps: list[tuple[str, str]] | None = None,
    ) -> None:
        """Store an entry by location string.

        This satisfies the :class:`Index` ABC contract.  For file-based
        workflows prefer :meth:`put_file` which computes hashes and
        mtimes automatically.  This method writes the entry with an
        empty content hash and no mtime caching.
        """
        with self.conn:
            self.conn.execute(
                """INSERT INTO index_entries
                       (grp, location, asset_id, fingerprint, content_hash)
                   VALUES (?, ?, ?, ?, '')
                   ON CONFLICT(grp, location) DO UPDATE SET
                       asset_id     = excluded.asset_id,
                       fingerprint  = excluded.fingerprint""",
                (group, location, asset_id, fingerprint),
            )
            self.conn.execute(
                "DELETE FROM index_deps WHERE entry_grp = ? AND entry_location = ?",
                (group, location),
            )
            if deps:
                self.conn.executemany(
                    """INSERT INTO index_deps
                           (entry_grp, entry_location,
                            dep_grp, dep_location,
                            dep_kind, dep_hash)
                       VALUES (?, ?, ?, ?, ?, '')""",
                    (
                        (group, location, group, dep_loc, dep_kind)
                        for dep_loc, dep_kind in deps
                    ),
                )

    def remove(self, location: str, *, group: str = "") -> bool:
        """Remove an entry and its deps (via CASCADE)."""
        cur = self.conn.execute(
            "DELETE FROM index_entries WHERE grp = ? AND location = ?",
            (group, location),
        )
        self.conn.commit()
        self._mtime_cache.discard(self._cache_key(group, location))
        self._mtime_cache.flush()
        return (cur.rowcount or 0) > 0

    def stale_entries(
        self,
        changed_locations: set[str],
        *,
        group: str = "",
    ) -> set[str]:
        """Return entry locations that depend on any changed locations."""
        if not changed_locations:
            return set()
        keys = list(changed_locations)
        result: set[str] = set()
        batch_size = 500
        for i in range(0, len(keys), batch_size):
            batch = keys[i : i + batch_size]
            placeholders = ",".join("?" for _ in batch)
            rows = self.conn.execute(
                "SELECT DISTINCT entry_location FROM index_deps "
                f"WHERE dep_grp = ? AND dep_location IN ({placeholders})",
                [group, *batch],
            ).fetchall()
            result.update(row["entry_location"] for row in rows)
        return result

    # ── File-specific API ────────────────────────────────────

    def flush_cache(self) -> None:
        """Persist the mtime cache to disk.

        Call once after a batch of :meth:`put_file` calls with
        ``flush=False`` to avoid repeated full-cache rewrites.
        """
        self._mtime_cache.flush()

    def put_file(
        self,
        source_path: Path,
        root: Path,
        *,
        group: str = "",
        asset_id: str,
        fingerprint: str,
        deps: list[tuple[Path, str]] | None = None,
        flush: bool = True,
    ) -> None:
        """Store a file entry with automatic hash computation.

        Mtimes are recorded in the local cache (never in SQLite)
        so that mtime-only changes don't dirty the shared state.db.

        Args:
            source_path: Path to the source file.
            root: Project root for computing the relative location key.
            group: Logical namespace for this entry.
            asset_id: Asset id this file produces.
            fingerprint: Content fingerprint of the compiled asset.
            deps: ``(file_path, kind)`` tuples.  Content hash
                is computed automatically from each dependency file.
            flush: Whether to persist the mtime cache to disk
                immediately.  Pass ``False`` during batch indexing
                and call :meth:`flush_cache` once at the end.
        """
        location = self._rel_path(source_path, root)
        cache_key = self._cache_key(group, location)
        with self.conn:
            self.conn.execute(
                """INSERT INTO index_entries
                       (grp, location, asset_id, fingerprint, content_hash)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(grp, location) DO UPDATE SET
                       asset_id     = excluded.asset_id,
                       fingerprint  = excluded.fingerprint,
                       content_hash = excluded.content_hash""",
                (
                    group,
                    location,
                    asset_id,
                    fingerprint,
                    self._content_hash(source_path),
                ),
            )
            # Store mtime in local cache (never in SQLite)
            self._mtime_cache.set(cache_key, self._mtime_ns(source_path))

            self.conn.execute(
                "DELETE FROM index_deps WHERE entry_grp = ? AND entry_location = ?",
                (group, location),
            )
            if deps:
                self.conn.executemany(
                    """INSERT INTO index_deps
                           (entry_grp, entry_location,
                            dep_grp, dep_location,
                            dep_kind, dep_hash)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (
                        (
                            group,
                            location,
                            group,
                            self._rel_path(dep_path, root),
                            kind,
                            self._content_hash(dep_path),
                        )
                        for dep_path, kind in deps
                    ),
                )
                # Cache dep mtimes locally
                for dep_path, _kind in deps:
                    dep_loc = self._rel_path(dep_path, root)
                    dep_cache_key = self._cache_key(group, dep_loc)
                    self._mtime_cache.set(dep_cache_key, self._mtime_ns(dep_path))

        if flush:
            self._mtime_cache.flush()

    def diff(
        self,
        discovered: list[tuple[Path, int]],
        root: Path,
        *,
        group: str = "",
    ) -> IndexStatus:
        """Classify discovered files into fresh / changed / new / deleted.

        Args:
            discovered: ``(path, mtime_ns)`` pairs from
                :class:`~assets.loader.discovery.FileDiscovery`.
            root: Project root for relative path computation.
            group: Logical namespace to scope the diff to.

        Returns:
            An :class:`IndexStatus` with four buckets.  Fresh entries
            include the ``asset_id`` so consumers can look up the
            compiled data from the state backend.
        """
        # 1. Load indexed entries for this group (hash only, no mtime)
        rows = self.conn.execute(
            "SELECT location, content_hash, asset_id FROM index_entries WHERE grp = ?",
            (group,),
        ).fetchall()
        indexed: dict[str, tuple[str, str]] = {
            r["location"]: (r["content_hash"], r["asset_id"]) for r in rows
        }

        # 2. Load dep edges for this group (hash only, no mtime)
        dep_rows = self.conn.execute(
            "SELECT entry_location, dep_grp, dep_location, dep_hash "
            "FROM index_deps WHERE entry_grp = ?",
            (group,),
        ).fetchall()
        deps_by_entry: dict[str, list[tuple[str, str, str]]] = {}
        all_dep_locs: set[tuple[str, str]] = set()
        for r in dep_rows:
            deps_by_entry.setdefault(r["entry_location"], []).append(
                (r["dep_grp"], r["dep_location"], r["dep_hash"])
            )
            all_dep_locs.add((r["dep_grp"], r["dep_location"]))

        # 3. Stat unique dep files once
        dep_current: dict[tuple[str, str], tuple[int, str | None]] = {}
        for dep_grp, dep_loc in all_dep_locs:
            try:
                dep_current[(dep_grp, dep_loc)] = (
                    (root / dep_loc).stat().st_mtime_ns,
                    None,
                )
            except OSError:
                dep_current[(dep_grp, dep_loc)] = (-1, None)

        # 4. Classify
        status = IndexStatus()
        seen: set[str] = set()
        cache_dirty = False

        for path, mtime_ns in discovered:
            location = self._rel_path(path, root)
            seen.add(location)

            entry = indexed.get(location)
            if entry is None:
                status.new.append(path)
                continue

            stored_hash, asset_id = entry

            fresh, updated = self._check_source_fresh(
                self._cache_key(group, location),
                mtime_ns,
                stored_hash,
                path,
            )
            if not fresh:
                status.changed.append(path)
                continue
            if updated:
                cache_dirty = True

            entry_deps = deps_by_entry.get(location, [])
            deps_fresh, deps_updated = self._check_deps_fresh(
                entry_deps,
                dep_current,
                root,
            )
            if deps_fresh:
                status.fresh.append((path, asset_id))
            else:
                status.changed.append(path)
            if deps_updated:
                cache_dirty = True

        # 5. Remaining -> deleted
        for location, (_h, asset_id) in indexed.items():
            if location not in seen:
                status.deleted.append((location, asset_id))

        # Flush mtime cache if any mtimes were updated
        if cache_dirty:
            self._mtime_cache.flush()

        return status

    def clean(self, root: Path, *, group: str = "") -> int:
        """Remove entries whose source files no longer exist.

        Returns count of removed entries.
        """
        rows = self.conn.execute(
            "SELECT location FROM index_entries WHERE grp = ?",
            (group,),
        ).fetchall()
        to_delete: list[str] = []
        for row in rows:
            loc = row["location"]
            if (root / loc).exists():
                continue
            stem_path = Path(loc)
            found = any(
                (root / stem_path.with_suffix(ext)).exists()
                for ext in _SOURCE_EXTENSIONS
                if ext != stem_path.suffix
            )
            if not found:
                to_delete.append(loc)

        if to_delete:
            placeholders = ",".join("?" for _ in to_delete)
            self.conn.execute(
                "DELETE FROM index_entries "
                f"WHERE grp = ? AND location IN ({placeholders})",
                [group, *to_delete],
            )
            self.conn.commit()
            for loc in to_delete:
                self._mtime_cache.discard(self._cache_key(group, loc))
            self._mtime_cache.flush()
        return len(to_delete)

    # ── Internal freshness checks ────────────────────────────

    def _check_source_fresh(
        self,
        cache_key: str,
        current_mtime: int,
        stored_hash: str,
        source_path: Path,
    ) -> tuple[bool, bool]:
        """Two-tier source freshness: mtime fast path, hash fallback.

        Returns:
            ``(is_fresh, cache_updated)`` — second element is True
            when the local mtime cache was updated (hash matched but
            mtime differed).
        """
        cached_mtime = self._mtime_cache.get(cache_key)
        if cached_mtime is not None and current_mtime == cached_mtime:
            return True, False

        current_hash = self._content_hash(source_path)
        if current_hash == stored_hash:
            # Content unchanged — update local mtime cache only
            self._mtime_cache.set(cache_key, current_mtime)
            return True, True

        return False, False

    def _check_deps_fresh(
        self,
        entry_deps: list[tuple[str, str, str]],
        dep_current: dict[tuple[str, str], tuple[int, str | None]],
        root: Path,
    ) -> tuple[bool, bool]:
        """Check all dependencies for an entry.

        Returns:
            ``(all_fresh, cache_updated)`` — second element is True
            when any dep's local mtime cache was updated.
        """
        any_updated = False
        for dep_grp, dep_loc, stored_hash in entry_deps:
            current = dep_current.get((dep_grp, dep_loc))
            if current is None or current[0] == -1:
                return False, any_updated
            current_mtime, cached_hash = current

            # Mtime fast path from local cache
            dep_cache_key = self._cache_key(dep_grp, dep_loc)
            cached_mtime = self._mtime_cache.get(dep_cache_key)
            if cached_mtime is not None and current_mtime == cached_mtime:
                continue

            # Hash fallback
            if cached_hash is not None:
                current_hash = cached_hash
            else:
                try:
                    current_hash = self._content_hash(root / dep_loc)
                except OSError:
                    return False, any_updated
            if current_hash != stored_hash:
                return False, any_updated
            # Hash matches — update local mtime cache
            self._mtime_cache.set(dep_cache_key, current_mtime)
            dep_current[(dep_grp, dep_loc)] = (current_mtime, current_hash)
            any_updated = True
        return True, any_updated
