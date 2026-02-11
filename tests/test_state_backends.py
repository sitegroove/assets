"""Tests for state backends (Memory and LocalJSON)."""

from pathlib import Path

import pytest

from assets import LocalJSONBackend, MemoryBackend
from assets.state.models import AssetState, StateSnapshot


class TestMemoryBackend:
    def test_load_empty(self):
        backend = MemoryBackend()
        assert backend.load("dev") is None

    def test_save_and_load(self):
        backend = MemoryBackend()
        state = StateSnapshot(environment="dev")
        backend.save("dev", state)
        loaded = backend.load("dev")
        assert loaded is not None
        assert loaded.environment == "dev"

    def test_list_environments(self):
        backend = MemoryBackend()
        backend.save("dev", StateSnapshot(environment="dev"))
        backend.save("prod", StateSnapshot(environment="prod"))
        envs = backend.list_environments()
        assert set(envs) == {"dev", "prod"}

    def test_delete_environment(self):
        backend = MemoryBackend()
        backend.save("dev", StateSnapshot(environment="dev"))
        backend.delete_environment("dev")
        assert backend.load("dev") is None

    def test_lock(self):
        backend = MemoryBackend()
        with backend.lock("dev"):
            pass  # should succeed

    def test_double_lock_raises(self):
        backend = MemoryBackend()
        with backend.lock("dev"):
            with pytest.raises(RuntimeError, match="already locked"):
                with backend.lock("dev"):
                    pass

    def test_lock_released_after_exception(self):
        backend = MemoryBackend()
        with pytest.raises(ValueError):
            with backend.lock("dev"):
                raise ValueError("test")
        # Lock should be released
        with backend.lock("dev"):
            pass


class TestLocalJSONBackend:
    def test_load_empty(self, tmp_path: Path):
        backend = LocalJSONBackend(state_dir=str(tmp_path))
        assert backend.load("dev") is None

    def test_save_and_load(self, tmp_path: Path):
        backend = LocalJSONBackend(state_dir=str(tmp_path))
        state = StateSnapshot(
            environment="dev",
            assets={"a": AssetState(name="a", fingerprint="fp1")},
        )
        backend.save("dev", state)
        loaded = backend.load("dev")
        assert loaded is not None
        assert "a" in loaded.assets

    def test_list_environments(self, tmp_path: Path):
        backend = LocalJSONBackend(state_dir=str(tmp_path))
        backend.save("dev", StateSnapshot(environment="dev"))
        backend.save("prod", StateSnapshot(environment="prod"))
        envs = backend.list_environments()
        assert set(envs) == {"dev", "prod"}

    def test_delete_environment(self, tmp_path: Path):
        backend = LocalJSONBackend(state_dir=str(tmp_path))
        backend.save("dev", StateSnapshot(environment="dev"))
        backend.delete_environment("dev")
        assert backend.load("dev") is None

    def test_lock(self, tmp_path: Path):
        backend = LocalJSONBackend(state_dir=str(tmp_path))
        with backend.lock("dev"):
            # Lock file should exist
            lock_path = tmp_path / "dev" / "state.json.lock"
            assert lock_path.exists()
        # Lock file should be cleaned up
        assert not lock_path.exists()

    def test_creates_directories(self, tmp_path: Path):
        state_dir = tmp_path / "deep" / "nested" / "path"
        backend = LocalJSONBackend(state_dir=str(state_dir))
        backend.save("dev", StateSnapshot(environment="dev"))
        assert backend.load("dev") is not None
