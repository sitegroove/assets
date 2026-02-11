"""FsspecBackend — cloud-agnostic state backend using fsspec.

Supports any fsspec-compatible filesystem: S3, GCS, local, Azure, etc.
Install extras: pip install assets[s3] or assets[gcs] or assets[cloud]
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Generator
from contextlib import contextmanager
from typing import Any

from assets.state.backend import StateBackend
from assets.state.models import StateSnapshot

logger = logging.getLogger(__name__)


class FsspecBackend(StateBackend):
    """State backend for any fsspec-supported filesystem (S3, GCS, local, etc).

    Usage:
        FsspecBackend("s3://my-bucket/assets-state")
        FsspecBackend("gcs://my-bucket/assets-state")
        FsspecBackend("file:///tmp/assets-state")
        FsspecBackend("/tmp/assets-state")  # local shorthand

    Any extra keyword arguments are passed to fsspec as storage_options
    (e.g., profile, endpoint_url, project, token).
    """

    def __init__(self, base_url: str, **storage_options: Any) -> None:
        try:
            import fsspec
        except ImportError as e:
            raise ImportError(
                "fsspec is required for FsspecBackend. "
                "Install it with: pip install 'assets[s3]' or 'assets[gcs]' or 'assets[cloud]'"
            ) from e

        self._base_url = base_url.rstrip("/")
        self._fs, self._root = fsspec.core.url_to_fs(base_url, **storage_options)
        self._root = self._root.rstrip("/")

    def _state_path(self, environment: str) -> str:
        return f"{self._root}/{environment}/state.json"

    def _lock_path(self, environment: str) -> str:
        return f"{self._root}/{environment}/state.json.lock"

    def load(self, environment: str) -> StateSnapshot | None:
        path = self._state_path(environment)
        try:
            if not self._fs.exists(path):
                return None
            data = json.loads(self._fs.cat_file(path))
            return StateSnapshot.model_validate(data)
        except FileNotFoundError:
            return None
        except json.JSONDecodeError:
            logger.warning("Corrupted state file at %s, returning None", path)
            return None

    def save(self, environment: str, state: StateSnapshot) -> None:
        path = self._state_path(environment)
        self._fs.mkdirs(f"{self._root}/{environment}", exist_ok=True)
        self._fs.pipe_file(path, state.model_dump_json(indent=2).encode())

    @contextmanager
    def lock(self, environment: str) -> Generator[None, None, None]:
        lock_path = self._lock_path(environment)
        self._fs.mkdirs(f"{self._root}/{environment}", exist_ok=True)

        max_retries = 10
        acquired = False
        for i in range(max_retries):
            if not self._fs.exists(lock_path):
                try:
                    self._fs.pipe_file(lock_path, str(time.time()).encode())
                    acquired = True
                    break
                except Exception:
                    # Race condition — another process created it first
                    pass

            if i == max_retries - 1:
                # Check for stale lock (older than 60s)
                try:
                    info = self._fs.info(lock_path)
                    # Different filesystems use different keys for mtime
                    mtime = (
                        info.get("LastModified")
                        or info.get("updated")
                        or info.get("mtime")
                    )
                    if mtime is not None:
                        if hasattr(mtime, "timestamp"):
                            lock_age = time.time() - mtime.timestamp()
                        else:
                            lock_age = time.time() - float(mtime)
                        if lock_age > 60:
                            logger.warning(
                                "Removing stale lock for '%s' (age=%.0fs)",
                                environment, lock_age,
                            )
                            self._fs.rm(lock_path)
                            continue
                except Exception:
                    pass
                raise RuntimeError(
                    f"Could not acquire lock for '{environment}' after {max_retries} retries"
                )
            time.sleep(0.1 * (i + 1))

        if not acquired:
            raise RuntimeError(
                f"Could not acquire lock for '{environment}' after {max_retries} retries"
            )

        try:
            yield
        finally:
            try:
                self._fs.rm(lock_path)
            except Exception:
                logger.warning("Failed to release lock at %s", lock_path)

    def list_environments(self) -> list[str]:
        try:
            entries = self._fs.ls(self._root, detail=False)
        except FileNotFoundError:
            return []
        envs = []
        for entry in entries:
            entry = entry.rstrip("/")
            name = entry.split("/")[-1]
            state_path = f"{entry}/state.json"
            try:
                if self._fs.exists(state_path):
                    envs.append(name)
            except Exception:
                continue
        return sorted(envs)

    def delete_environment(self, environment: str) -> None:
        env_dir = f"{self._root}/{environment}"
        try:
            if self._fs.exists(env_dir):
                self._fs.rm(env_dir, recursive=True)
        except FileNotFoundError:
            pass
