"""TieredBackend — local SQLite + remote sync via fsspec.

Local SQLite for fast reads, remote storage for persistence.
The remote path can be a cloud bucket (S3/GCS/Azure) or a local
directory (e.g. mounted network drive, attached S3 disk).
plan() never hits remote — always reads local. apply() syncs to both.

Remote layout:
    <remote_path>/
    ├── state.db           ← full SQLite database
    └── snapshot.json      ← lightweight sync metadata

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

    Sync protocol:
    - Remote stores: state.db + snapshot.json
    - On load(): check local snapshot fingerprint vs remote snapshot.json.
      If different, pull remote state.db first.
    - On save(): write to local SQLite, then push state.db + snapshot.json.
    - lock(): acquires remote lock file, then delegates to local SQLite lock.
    - sync(): public method to manually pull remote state.

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

        self._local_db_path = Path(local_path) / "state.db"
        self._local = SQLiteBackend(db_path=self._local_db_path)
        self._lock_timeout = lock_timeout
        self._lock_retries = lock_retries

        # Track local snapshot fingerprint for skip-sync optimization
        self._local_fingerprint: str = ""
        self._synced = False
        self._remote_locks: set[str] = set()

    # ─── Remote paths ──────────────────────────────────────

    @property
    def _remote_db_path(self) -> str:
        return f"{self._root}/state.db"

    @property
    def _remote_snapshot_path(self) -> str:
        return f"{self._root}/snapshot.json"

    def _remote_lock_path(self, environment: str) -> str:
        return f"{self._root}/{environment}.lock"

    # ─── Sync protocol ─────────────────────────────────────

    def _remote_snapshot(self) -> RemoteSnapshot | None:
        """Fetch remote snapshot.json. Returns None if not found."""
        try:
            if not self._fs.exists(self._remote_snapshot_path):
                return None
            data = self._fs.cat_file(self._remote_snapshot_path)
            return RemoteSnapshot.from_json(data)
        except FileNotFoundError:
            return None
        except Exception:
            logger.warning(
                "Failed to read remote snapshot at %s",
                self._remote_snapshot_path,
                exc_info=True,
            )
            return None

    def _needs_sync(self) -> bool:
        """Check whether local state is stale compared to remote.

        Compares local DB fingerprint against remote snapshot.json.
        Returns True if a pull is needed, False if local is up-to-date.
        """
        remote_snap = self._remote_snapshot()
        if remote_snap is None:
            # No remote state yet — local is authoritative
            return False

        local_fp = _db_fingerprint(self._local_db_path)
        if local_fp == remote_snap.fingerprint:
            return False

        return True

    def _ensure_synced(self) -> None:
        """Pull remote state if stale. Called before load()."""
        if self._synced:
            return
        if self._needs_sync():
            self.pull()
        self._synced = True

    def pull(self) -> None:
        """Download remote state.db to local, replacing the local copy.

        Closes the local SQLite connection before overwriting the file,
        then reopens on next access.
        """
        try:
            if not self._fs.exists(self._remote_db_path):
                logger.debug("No remote state.db found at %s", self._remote_db_path)
                return

            # Close local connection before overwriting
            self._local.close()

            self._local_db_path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = self._local_db_path.with_suffix(".db.download")
            try:
                self._fs.get_file(self._remote_db_path, str(tmp_path))
                shutil.move(str(tmp_path), str(self._local_db_path))
            except Exception:
                tmp_path.unlink(missing_ok=True)
                raise

            self._local_fingerprint = _db_fingerprint(self._local_db_path)
            logger.info("Pulled remote state.db → %s", self._local_db_path)

        except FileNotFoundError:
            logger.debug("Remote state.db not found, starting fresh")

    def push(self) -> None:
        """Upload local state.db and snapshot.json to remote.

        Flushes WAL to ensure the database file is self-contained,
        computes fingerprint, then uploads both files atomically.
        """
        # Flush WAL so the .db file is self-contained
        self._local.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")

        # Compute fingerprint BEFORE upload to avoid TOCTOU
        fp = _db_fingerprint(self._local_db_path)
        self._local_fingerprint = fp

        self._fs.mkdirs(self._root, exist_ok=True)

        # Upload state.db
        self._fs.put_file(str(self._local_db_path), self._remote_db_path)

        # Upload snapshot.json
        snapshot = RemoteSnapshot(
            fingerprint=fp,
            version=self._snapshot_version() + 1,
            updated_at=datetime.now(timezone.utc).isoformat(),
        )
        self._fs.pipe_file(self._remote_snapshot_path, snapshot.to_json())
        logger.info("Pushed local state.db → %s", self._remote_db_path)

    def _snapshot_version(self) -> int:
        """Get current remote snapshot version, or 0 if none exists."""
        snap = self._remote_snapshot()
        return snap.version if snap else 0

    def sync(self) -> None:
        """Public method: pull remote state if stale.

        Call this explicitly to refresh local state from remote
        without waiting for the next load() call.
        """
        self._synced = False
        self._ensure_synced()

    # ─── StateBackend interface ────────────────────────────

    def load(self, environment: str) -> StateSnapshot | None:
        """Load state from local SQLite, syncing from remote if stale."""
        self._ensure_synced()
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
        self.push()

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
            self._synced = False
            self._ensure_synced()

            with self._local.lock(environment):
                yield
        finally:
            self._release_remote_lock(environment)

    def list_environments(self) -> list[str]:
        """List environments from local SQLite (synced)."""
        self._ensure_synced()
        return self._local.list_environments()

    def delete_environment(self, environment: str) -> None:
        """Delete environment from local and push to remote."""
        self._local.delete_environment(environment)
        self.push()

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
        self._ensure_synced()
        return self._local.asset_history(environment, asset_id, limit=limit)

    def environment_changelog(
        self,
        environment: str,
        *,
        since: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Return environment changelog from local SQLite."""
        self._ensure_synced()
        return self._local.environment_changelog(environment, since=since, limit=limit)

    def asset_version(
        self,
        environment: str,
        asset_id: str,
        version: int,
    ) -> dict[str, Any] | None:
        """Retrieve a specific historical version from local SQLite."""
        self._ensure_synced()
        return self._local.asset_version(environment, asset_id, version)

    def prune_history(self, *, keep_days: int = 90) -> int:
        """Delete old history entries from local SQLite, then push."""
        count = self._local.prune_history(keep_days=keep_days)
        if count > 0:
            self.push()
        return count

    def close(self) -> None:
        """Close the local SQLite connection."""
        self._local.close()
