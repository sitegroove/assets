"""LocalJSONBackend — file-system state backend using JSON files."""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

from assets.state.backend import StateBackend
from assets.state.models import StateSnapshot

logger = logging.getLogger(__name__)


class LocalJSONBackend(StateBackend):
    """Stores state as JSON files on the local filesystem.

    Layout:
        <state_dir>/
        ├── production/
        │   ├── state.json
        │   └── state.json.lock
        └── staging/
            └── state.json
    """

    def __init__(self, state_dir: str = ".assets_state/environments") -> None:
        self._state_dir = Path(state_dir)

    def _env_dir(self, environment: str) -> Path:
        return self._state_dir / environment

    def _state_path(self, environment: str) -> Path:
        return self._env_dir(environment) / "state.json"

    def _lock_path(self, environment: str) -> Path:
        return self._env_dir(environment) / "state.json.lock"

    def load(self, environment: str) -> StateSnapshot | None:
        path = self._state_path(environment)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text())
            return StateSnapshot.model_validate(data)
        except json.JSONDecodeError:
            logger.warning("Corrupted state file at %s, returning None", path)
            return None
        except Exception:
            logger.warning("Failed to load state from %s", path, exc_info=True)
            return None

    def save(self, environment: str, state: StateSnapshot) -> None:
        path = self._state_path(environment)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(state.model_dump_json(indent=2))

    @contextmanager
    def lock(self, environment: str) -> Generator[None, None, None]:
        lock_path = self._lock_path(environment)
        lock_path.parent.mkdir(parents=True, exist_ok=True)

        # Simple file-based lock with retry
        max_retries = 10
        for i in range(max_retries):
            try:
                # O_CREAT | O_EXCL — atomic create, fails if exists
                fd = lock_path.open("x")
                fd.write(str(time.time()))
                fd.close()
                break
            except FileExistsError:
                if i == max_retries - 1:
                    # Check for stale lock (older than 60s)
                    try:
                        lock_age = time.time() - lock_path.stat().st_mtime
                        if lock_age > 60:
                            lock_path.unlink(missing_ok=True)
                            continue
                    except OSError:
                        pass
                    raise RuntimeError(
                        f"Could not acquire lock for '{environment}' after {max_retries} retries"
                    )
                time.sleep(0.1 * (i + 1))
        try:
            yield
        finally:
            lock_path.unlink(missing_ok=True)

    def list_environments(self) -> list[str]:
        if not self._state_dir.exists():
            return []
        return [
            d.name
            for d in sorted(self._state_dir.iterdir())
            if d.is_dir() and (d / "state.json").exists()
        ]

    def delete_environment(self, environment: str) -> None:
        env_dir = self._env_dir(environment)
        if env_dir.exists():
            import shutil

            shutil.rmtree(env_dir)
