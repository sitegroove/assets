"""Tests for FsspecBackend using local filesystem."""

from pathlib import Path

import pytest

from assets.state.fsspec import FsspecBackend
from assets.state.models import AssetState, StateSnapshot


class TestFsspecBackend:
    def test_load_empty(self, tmp_path: Path):
        backend = FsspecBackend(str(tmp_path))
        assert backend.load("dev") is None

    def test_save_and_load(self, tmp_path: Path):
        backend = FsspecBackend(str(tmp_path))
        state = StateSnapshot(
            environment="dev",
            assets={"a": AssetState(name="a", fingerprint="fp1")},
        )
        backend.save("dev", state)
        loaded = backend.load("dev")
        assert loaded is not None
        assert "a" in loaded.assets
        assert loaded.assets["a"].fingerprint == "fp1"

    def test_list_environments(self, tmp_path: Path):
        backend = FsspecBackend(str(tmp_path))
        backend.save("dev", StateSnapshot(environment="dev"))
        backend.save("prod", StateSnapshot(environment="prod"))
        envs = backend.list_environments()
        assert set(envs) == {"dev", "prod"}

    def test_list_environments_empty(self, tmp_path: Path):
        backend = FsspecBackend(str(tmp_path / "nonexistent"))
        assert backend.list_environments() == []

    def test_delete_environment(self, tmp_path: Path):
        backend = FsspecBackend(str(tmp_path))
        backend.save("dev", StateSnapshot(environment="dev"))
        backend.delete_environment("dev")
        assert backend.load("dev") is None

    def test_delete_nonexistent_environment(self, tmp_path: Path):
        backend = FsspecBackend(str(tmp_path))
        # Should not raise
        backend.delete_environment("nonexistent")

    def test_lock(self, tmp_path: Path):
        backend = FsspecBackend(str(tmp_path))
        with backend.lock("dev"):
            # Lock file should exist during the context
            lock_path = tmp_path / "dev" / "state.json.lock"
            assert lock_path.exists()
        # Lock file should be cleaned up after
        assert not lock_path.exists()

    def test_lock_released_after_exception(self, tmp_path: Path):
        backend = FsspecBackend(str(tmp_path))
        with pytest.raises(ValueError):
            with backend.lock("dev"):
                raise ValueError("test")
        # Lock should be released — can lock again
        with backend.lock("dev"):
            pass

    def test_creates_directories(self, tmp_path: Path):
        state_dir = tmp_path / "deep" / "nested" / "path"
        backend = FsspecBackend(str(state_dir))
        backend.save("dev", StateSnapshot(environment="dev"))
        assert backend.load("dev") is not None

    def test_corrupted_state_returns_none(self, tmp_path: Path):
        backend = FsspecBackend(str(tmp_path))
        # Write corrupted JSON
        env_dir = tmp_path / "dev"
        env_dir.mkdir(parents=True)
        (env_dir / "state.json").write_text("not valid json{{{")
        assert backend.load("dev") is None

    def test_with_file_protocol(self, tmp_path: Path):
        backend = FsspecBackend(f"file://{tmp_path}")
        state = StateSnapshot(environment="test")
        backend.save("test", state)
        loaded = backend.load("test")
        assert loaded is not None
        assert loaded.environment == "test"

    def test_overwrite_state(self, tmp_path: Path):
        backend = FsspecBackend(str(tmp_path))
        state1 = StateSnapshot(environment="dev")
        backend.save("dev", state1)

        state2 = StateSnapshot(
            environment="dev",
            assets={"b": AssetState(name="b", fingerprint="fp2")},
        )
        backend.save("dev", state2)

        loaded = backend.load("dev")
        assert loaded is not None
        assert "b" in loaded.assets
