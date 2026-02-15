"""TieredBackend — local SQLite + remote sync via fsspec.

Local SQLite for fast reads, remote storage for persistence.
The remote path can be a cloud bucket (S3/GCS/Azure) or a local
directory (e.g. mounted network drive, attached S3 disk).
plan() never hits remote — always reads local. apply() syncs to both.

Remote layout (per-environment)::

    <remote_path>/
    ├── production/
    │   ├── state.db
    │   └── snapshot.json
    ├── staging/
    │   ├── state.db
    │   └── snapshot.json
    └── dev/
        ├── state.db
        └── snapshot.json

snapshot.json:
    {"fingerprint": "<sha256-of-db>", "version": <int>, "updated_at": "<iso>"}
"""

from __future__ import annotations

import hashlib
import json
import logging
import shutil
import time
from collections.abc import Generator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from assets.state.backend import StateBackend
from assets.state.models import StateSnapshot
from assets.state.sqlite import SQLiteBackend

logger = logging.getLogger(__name__)

_DEFAULT_LOCK_STALE_SECONDS = 300
_DEFAULT_LOCK_MAX_RETRIES = 10


def _db_fingerprint(db_path: Path) -> str:
    """Compute SHA-256 fingerprint of a database file."""
    if not db_path.exists():
        return ""
    h = hashlib.sha256()
    with open(db_path, "rb") as f:
        while True:
            chunk = f.read(8192)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


class RemoteSnapshot:
    """Lightweight sync metadata stored alongside the remote state.db."""

    def __init__(
        self,
        fingerprint: str = "",
        version: int = 0,
        updated_at: str = "",
    ) -> None:
        self.fingerprint = fingerprint
        self.version = version
        self.updated_at = updated_at or datetime.now(timezone.utc).isoformat()

    def to_json(self) -> bytes:
        return json.dumps(
            {
                "fingerprint": self.fingerprint,
                "version": self.version,
                "updated_at": self.updated_at,
            },
            indent=2,
        ).encode()

    @classmethod
    def from_json(cls, data: bytes) -> RemoteSnapshot:
        parsed = json.loads(data)
        return cls(
            fingerprint=parsed.get("fingerprint", ""),
            version=parsed.get("version", 0),
            updated_at=parsed.get("updated_at", ""),
        )


class TieredBackend(StateBackend):
    """Local SQLite for reads, remote storage for persistence.

    The remote path can be a cloud bucket (``s3://``, ``gcs://``,
    ``az://``) **or** a plain local directory (e.g. a mounted
    network drive or S3 volume).

    Each environment has its own ``state.db`` and ``snapshot.json``
    under a dedicated subdirectory (both locally and remotely),
    providing full physical isolation between environments.

    Sync protocol (per environment):
    - Remote stores: ``{env}/state.db`` + ``{env}/snapshot.json``
    - On ``load()``: check local fingerprint vs remote snapshot.
      If different, pull remote state.db first.
    - On ``save()``: write to local SQLite, then push state.db +
      snapshot.json.
    - ``lock()``: acquires remote lock file, then delegates to
      local SQLite lock.
    - ``sync()``: public method to manually pull remote state.

    Usage::

        TieredBackend("s3://my-bucket/assets-state")
        TieredBackend("gcs://my-bucket/assets-state")
        TieredBackend("/mnt/shared/state", local_path=".assets_state")

    Any extra keyword arguments are passed to fsspec as storage_options
    (e.g., profile, endpoint_url, project, token).
    """

    def __init__(
        self,
        remote_path: str,
        *,
        local_path: str | Path = ".assets_state",
        lock_timeout: int = _DEFAULT_LOCK_STALE_SECONDS,
        lock_retries: int = _DEFAULT_LOCK_MAX_RETRIES,
        **storage_options: Any,
    ) -> None:
        import fsspec

        self._remote_path = remote_path.rstrip("/")
        self._fs, self._root = fsspec.core.url_to_fs(remote_path, **storage_options)
        self._root = self._root.rstrip("/")

        self._local = SQLiteBackend(base_path=local_path)
        self._lock_timeout = lock_timeout
        self._lock_retries = lock_retries

        # Per-environment sync tracking
        self._local_fingerprints: dict[str, str] = {}
        self._synced: dict[str, bool] = {}
        self._remote_locks: set[str] = set()

    # ─── Remote paths (per-environment) ────────────────────

    def _remote_env_dir(self, environment: str) -> str:
        return f"{self._root}/{environment}"

    def _remote_db_path(self, environment: str) -> str:
        return f"{self._root}/{environment}/state.db"

    def _remote_snapshot_path(self, environment: str) -> str:
        return f"{self._root}/{environment}/snapshot.json"

    def _remote_lock_path(self, environment: str) -> str:
        return f"{self._root}/{environment}.lock"

    # ─── Local paths ───────────────────────────────────────

    def _local_db_path(self, environment: str) -> Path:
        return self._local.env_db_path(environment)

    # ─── Sync protocol (per-environment) ───────────────────

    def _remote_snapshot(self, environment: str) -> RemoteSnapshot | None:
        """Fetch remote snapshot.json for an environment."""
        snapshot_path = self._remote_snapshot_path(environment)
        try:
            if not self._fs.exists(snapshot_path):
                return None
            data = self._fs.cat_file(snapshot_path)
            return RemoteSnapshot.from_json(data)
        except FileNotFoundError:
            return None
        except Exception:
            logger.warning(
                "Failed to read remote snapshot at %s",
                snapshot_path,
                exc_info=True,
            )
            return None

    def _needs_sync(self, environment: str) -> bool:
        """Check whether local state for an env is stale vs remote.

        Compares local DB fingerprint against remote snapshot.json.
        Returns True if a pull is needed, False if local is up-to-date.
        """
        remote_snap = self._remote_snapshot(environment)
        if remote_snap is None:
            # No remote state yet — local is authoritative
            return False

        local_db = self._local_db_path(environment)
        local_fp = _db_fingerprint(local_db)
        if local_fp == remote_snap.fingerprint:
            return False

        return True

    def _ensure_synced(self, environment: str) -> None:
        """Pull remote state for an env if stale. Called before load()."""
        if self._synced.get(environment, False):
            return
        if self._needs_sync(environment):
            self.pull(environment)
        self._synced[environment] = True

    def pull(self, environment: str) -> None:
        """Download remote state.db to local for a specific environment.

        Closes the local SQLite connection before overwriting the file,
        then reopens on next access.
        """
        remote_db = self._remote_db_path(environment)
        try:
            if not self._fs.exists(remote_db):
                logger.debug("No remote state.db found at %s", remote_db)
                return

            # Close local connection before overwriting
            self._local.close_env(environment)

            local_db = self._local_db_path(environment)
            local_db.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = local_db.with_suffix(".db.download")
            try:
                self._fs.get_file(remote_db, str(tmp_path))
                shutil.move(str(tmp_path), str(local_db))
            except Exception:
                tmp_path.unlink(missing_ok=True)
                raise

            self._local_fingerprints[environment] = _db_fingerprint(local_db)
            logger.info("Pulled remote state.db for '%s' → %s", environment, local_db)

        except FileNotFoundError:
            logger.debug(
                "Remote state.db not found for '%s', starting fresh", environment
            )

    def push(self, environment: str) -> None:
        """Upload local state.db and snapshot.json to remote for an env.

        Flushes WAL to ensure the database file is self-contained,
        computes fingerprint, then uploads both files.
        """
        local_db = self._local_db_path(environment)

        # Flush WAL so the .db file is self-contained
        self._local.conn(environment).execute("PRAGMA wal_checkpoint(TRUNCATE)")

        # Compute fingerprint BEFORE upload to avoid TOCTOU
        fp = _db_fingerprint(local_db)
        self._local_fingerprints[environment] = fp

        env_dir = self._remote_env_dir(environment)
        self._fs.mkdirs(env_dir, exist_ok=True)

        # Upload state.db
        self._fs.put_file(str(local_db), self._remote_db_path(environment))

        # Upload snapshot.json
        snapshot = RemoteSnapshot(
            fingerprint=fp,
            version=self._snapshot_version(environment) + 1,
            updated_at=datetime.now(timezone.utc).isoformat(),
        )
        self._fs.pipe_file(self._remote_snapshot_path(environment), snapshot.to_json())
        logger.info(
            "Pushed local state.db for '%s' → %s",
            environment,
            self._remote_db_path(environment),
        )

    def _snapshot_version(self, environment: str) -> int:
        """Get current remote snapshot version for an env, or 0."""
        snap = self._remote_snapshot(environment)
        return snap.version if snap else 0

    def sync(self, environment: str | None = None) -> None:
        """Public method: pull remote state if stale.

        Call this explicitly to refresh local state from remote
        without waiting for the next load() call.

        When *environment* is ``None``, syncs all known environments.
        """
        if environment is not None:
            self._synced.pop(environment, None)
            self._ensure_synced(environment)
        else:
            self._synced.clear()
            for env in self.list_environments():
                self._ensure_synced(env)

    # ─── StateBackend interface ────────────────────────────

    def load(self, environment: str) -> StateSnapshot | None:
        """Load state from local SQLite, syncing from remote if stale."""
        self._ensure_synced(environment)
        return self._local.load(environment)

    def save(
        self,
        environment: str,
        state: StateSnapshot,
        *,
        changed_ids: set[str] | None = None,
    ) -> None:
        """Save state to local SQLite, then push to remote."""
        self._local.save(environment, state, changed_ids=changed_ids)
        self.push(environment)

    @contextmanager
    def lock(self, environment: str) -> Generator[None, None, None]:
        """Acquire remote lock, sync from remote, yield, release.

        Lock flow:
        1. Acquire remote lock file (fsspec-based)
        2. Pull remote → local (if stale)
        3. Yield (caller does work; save() pushes to remote)
        4. Release remote lock

        Note: pushing to remote is handled by save(), not lock().
        The lock ensures exclusive access during the critical section.
        """
        self._acquire_remote_lock(environment)
        try:
            # Sync after acquiring lock to get latest state
            self._synced.pop(environment, None)
            self._ensure_synced(environment)

            with self._local.lock(environment):
                yield
        finally:
            self._release_remote_lock(environment)

    def list_environments(self) -> list[str]:
        """List environments from remote storage.

        Scans for subdirectories containing ``state.db``.
        Falls back to local listing if remote is unavailable.
        """
        try:
            entries = self._fs.ls(self._root, detail=False)
            envs: list[str] = []
            for entry in entries:
                entry_str = str(entry).rstrip("/")
                env_name = entry_str.rsplit("/", 1)[-1]
                # Skip lock files
                if env_name.endswith(".lock"):
                    continue
                db_path = f"{entry_str}/state.db"
                try:
                    if self._fs.exists(db_path):
                        envs.append(env_name)
                except Exception:
                    continue
            return sorted(envs)
        except FileNotFoundError:
            return []
        except Exception:
            logger.warning(
                "Failed to list remote environments, falling back to local",
                exc_info=True,
            )
            return self._local.list_environments()

    def delete_environment(self, environment: str) -> None:
        """Delete environment from local and remote storage."""
        # Delete local
        self._local.delete_environment(environment)
        self._synced.pop(environment, None)
        self._local_fingerprints.pop(environment, None)

        # Delete remote
        try:
            env_dir = self._remote_env_dir(environment)
            if self._fs.exists(env_dir):
                self._fs.rm(env_dir, recursive=True)
        except Exception:
            logger.warning(
                "Failed to delete remote state for '%s'",
                environment,
                exc_info=True,
            )

    def copy_environment(self, source: str, target: str) -> None:
        """Copy state from one environment to another.

        Ensures the source is synced locally, copies the local DB
        file, then pushes the target to remote.  If the source
        environment has no state, this is a no-op (consistent with
        ``SQLiteBackend.copy_environment``).
        """
        self._ensure_synced(source)
        self._local.copy_environment(source, target)

        # Only push if the copy actually produced a target DB.
        # When source doesn't exist, local copy is a no-op and
        # pushing would create an empty remote environment.
        target_db = self._local_db_path(target)
        if target_db.exists():
            self.push(target)

    # ─── Remote locking ────────────────────────────────────

    def _acquire_remote_lock(self, environment: str) -> None:
        """Create a remote lock file with retries and stale detection.

        Uses a write-then-verify pattern to mitigate TOCTOU race
        conditions on cloud storage where atomic create-if-not-exists
        is not available:
        1. Try to write lock file with our unique token
        2. Read back and verify our token is present
        3. If another process won, back off and retry

        Stale locks older than lock_timeout are automatically removed.
        """
        if environment in self._remote_locks:
            raise RuntimeError(
                f"Environment '{environment}' is already locked by this backend"
            )
        lock_path = self._remote_lock_path(environment)
        self._fs.mkdirs(self._root, exist_ok=True)
        token = f"{time.time()}:{id(self)}"

        attempt = 0
        while attempt < self._lock_retries:
            # Check for existing lock
            lock_exists = False
            try:
                lock_exists = self._fs.exists(lock_path)
            except FileNotFoundError:
                lock_exists = False

            if lock_exists:
                # Check if the lock is stale
                if self._try_remove_stale_lock(lock_path, environment):
                    # Stale lock removed — count as an attempt to avoid
                    # infinite loops if locks keep reappearing.
                    attempt += 1
                    continue

                # Lock held by another process, wait and retry
                attempt += 1
                if attempt >= self._lock_retries:
                    break
                time.sleep(0.1 * attempt)
                continue

            # No lock exists — try to acquire
            try:
                self._fs.pipe_file(lock_path, token.encode())
            except Exception as exc:
                logger.debug("Failed to write lock file: %s", exc)
                attempt += 1
                if attempt >= self._lock_retries:
                    break
                time.sleep(0.1 * attempt)
                continue

            # Verify we own the lock (mitigates TOCTOU race)
            try:
                content = self._fs.cat_file(lock_path)
                if content.decode().strip() == token:
                    self._remote_locks.add(environment)
                    return  # Lock acquired successfully
            except Exception as exc:
                logger.debug("Failed to verify lock ownership: %s", exc)

            # Another process won the race
            attempt += 1
            if attempt >= self._lock_retries:
                break
            time.sleep(0.1 * attempt)

        raise RuntimeError(
            f"Could not acquire remote lock for '{environment}' "
            f"after {self._lock_retries} retries"
        )

    def _try_remove_stale_lock(self, lock_path: str, environment: str) -> bool:
        """Check if a lock file is stale and remove it.

        Returns True if a stale lock was removed, False otherwise.
        """
        try:
            info = self._fs.info(lock_path)
        except FileNotFoundError:
            return True  # Lock disappeared, treat as removed
        except Exception as exc:
            logger.debug(
                "Cannot inspect lock for '%s': %s",
                environment,
                exc,
            )
            return False

        mtime = info.get("LastModified") or info.get("updated") or info.get("mtime")
        if mtime is None:
            return False

        if hasattr(mtime, "timestamp"):
            lock_age = time.time() - mtime.timestamp()
        else:
            lock_age = time.time() - float(mtime)

        if lock_age > self._lock_timeout:
            logger.warning(
                "Removing stale lock for '%s' (age=%.0fs)",
                environment,
                lock_age,
            )
            try:
                self._fs.rm(lock_path)
            except Exception as exc:
                logger.warning("Failed to remove stale lock: %s", exc)
                return False
            return True

        return False

    def _release_remote_lock(self, environment: str) -> None:
        """Remove the remote lock file."""
        self._remote_locks.discard(environment)
        lock_path = self._remote_lock_path(environment)
        try:
            self._fs.rm(lock_path)
        except Exception:
            logger.warning("Failed to release remote lock at %s", lock_path)

    # ─── History delegation ────────────────────────────────

    def asset_history(
        self,
        environment: str,
        asset_id: str,
        *,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Return version history from local SQLite."""
        self._ensure_synced(environment)
        return self._local.asset_history(environment, asset_id, limit=limit)

    def environment_changelog(
        self,
        environment: str,
        *,
        since: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Return environment changelog from local SQLite."""
        self._ensure_synced(environment)
        return self._local.environment_changelog(environment, since=since, limit=limit)

    def asset_version(
        self,
        environment: str,
        asset_id: str,
        version: int,
    ) -> dict[str, Any] | None:
        """Retrieve a specific historical version from local SQLite."""
        self._ensure_synced(environment)
        return self._local.asset_version(environment, asset_id, version)

    def prune_history(self, *, keep_days: int = 90) -> int:
        """Delete old history entries from local SQLite, then push."""
        envs = self._local.list_environments()
        total = 0
        for env in envs:
            c = self._local.conn(env)
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
            env_total = (cur.rowcount or 0) + (dep_cur.rowcount or 0)
            c.commit()
            if env_total > 0:
                self.push(env)
            total += env_total
        return total

    def close(self) -> None:
        """Close the local SQLite connections."""
        self._local.close()
