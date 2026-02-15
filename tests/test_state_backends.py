"""Tests for state backends (SQLite in-memory, SQLite file, and Tiered)."""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from assets import SQLiteBackend
from assets.state.models import AssetState, StateSnapshot

# ─── SQLiteBackend(:memory:) ──────────────────────────────


class TestMemoryBackend:
    """Tests for SQLiteBackend in :memory: mode."""

    def test_load_empty(self) -> None:
        backend = SQLiteBackend.memory()
        assert backend.load("dev") is None

    def test_save_and_load(self) -> None:
        backend = SQLiteBackend.memory()
        state = StateSnapshot(environment="dev")
        backend.save("dev", state)
        loaded = backend.load("dev")
        assert loaded is not None
        assert loaded.environment == "dev"

    def test_list_environments(self) -> None:
        backend = SQLiteBackend.memory()
        backend.save("dev", StateSnapshot(environment="dev"))
        backend.save("prod", StateSnapshot(environment="prod"))
        envs = backend.list_environments()
        assert set(envs) == {"dev", "prod"}

    def test_delete_environment(self) -> None:
        backend = SQLiteBackend.memory()
        backend.save("dev", StateSnapshot(environment="dev"))
        backend.delete_environment("dev")
        assert backend.load("dev") is None

    def test_lock(self) -> None:
        backend = SQLiteBackend.memory()
        with backend.lock("dev"):
            pass  # should succeed

    def test_double_lock_raises(self) -> None:
        backend = SQLiteBackend.memory()
        with backend.lock("dev"):
            with pytest.raises(RuntimeError, match="already locked"):
                with backend.lock("dev"):
                    pass

    def test_lock_released_after_exception(self) -> None:
        backend = SQLiteBackend.memory()
        with pytest.raises(ValueError, match="test"):
            with backend.lock("dev"):
                raise ValueError("test")
        # Lock should be released
        with backend.lock("dev"):
            pass

    def test_memory_factory(self) -> None:
        """SQLiteBackend.memory() creates an in-memory instance."""
        backend = SQLiteBackend.memory()
        assert backend._in_memory is True
        assert backend.local_path is None

    def test_memory_has_history(self) -> None:
        """In-memory backend gets free history via triggers."""
        backend = SQLiteBackend.memory()
        state = StateSnapshot(
            environment="dev",
            assets={"a": AssetState(id="a", fingerprint="fp1")},
        )
        backend.save("dev", state)
        history = backend.asset_history("dev", "a")
        assert len(history) == 1
        assert history[0]["action"] == "create"


# ─── SQLiteBackend ─────────────────────────────────────────
# Comprehensive SQLiteBackend tests live in test_sqlite_backend.py.
# This section only tests the StateBackend interface contract
# (load/save/lock/list/delete) to ensure parity with the in-memory backend.


# ─── TieredBackend ─────────────────────────────────────────


class TestTieredBackend:
    """Test TieredBackend using fsspec's local filesystem."""

    def _make_backend(self, tmp_path: Path) -> Any:
        from assets.state.tiered import TieredBackend

        remote_dir = tmp_path / "remote"
        remote_dir.mkdir()
        local_dir = tmp_path / "local"
        return TieredBackend(
            str(remote_dir),
            local_path=local_dir,
        )

    def test_load_empty(self, tmp_path: Path) -> None:
        backend = self._make_backend(tmp_path)
        assert backend.load("dev") is None

    def test_save_and_load(self, tmp_path: Path) -> None:
        backend = self._make_backend(tmp_path)
        state = StateSnapshot(
            environment="dev",
            assets={"a": AssetState(id="a", fingerprint="fp1")},
        )
        backend.save("dev", state)
        loaded = backend.load("dev")
        assert loaded is not None
        assert "a" in loaded.assets

    def test_push_creates_remote_files(self, tmp_path: Path) -> None:
        """Remote files are created under {root}/{env}/."""
        backend = self._make_backend(tmp_path)
        state = StateSnapshot(environment="dev")
        backend.save("dev", state)

        # Verify remote files exist at per-environment paths
        remote_dir = tmp_path / "remote"
        assert (remote_dir / "dev" / "state.db").exists()
        assert (remote_dir / "dev" / "snapshot.json").exists()

        # Verify snapshot.json content
        snap = json.loads((remote_dir / "dev" / "snapshot.json").read_text())
        assert "fingerprint" in snap
        assert snap["version"] >= 1

    def test_push_creates_separate_env_dirs(self, tmp_path: Path) -> None:
        """Each environment gets its own remote subdirectory."""
        backend = self._make_backend(tmp_path)
        backend.save("dev", StateSnapshot(environment="dev"))
        backend.save("prod", StateSnapshot(environment="prod"))

        remote_dir = tmp_path / "remote"
        assert (remote_dir / "dev" / "state.db").exists()
        assert (remote_dir / "prod" / "state.db").exists()
        assert (remote_dir / "dev" / "snapshot.json").exists()
        assert (remote_dir / "prod" / "snapshot.json").exists()

    def test_pull_syncs_from_remote(self, tmp_path: Path) -> None:
        """A second TieredBackend instance can pull state from remote."""
        backend1 = self._make_backend(tmp_path)
        state = StateSnapshot(
            environment="prod",
            assets={"x": AssetState(id="x", fingerprint="fp_x")},
        )
        backend1.save("prod", state)

        # Second instance pointing to same remote, different local
        from assets.state.tiered import TieredBackend

        remote_dir = tmp_path / "remote"
        local_dir2 = tmp_path / "local2"
        backend2 = TieredBackend(str(remote_dir), local_path=local_dir2)

        loaded = backend2.load("prod")
        assert loaded is not None
        assert "x" in loaded.assets
        assert loaded.assets["x"].fingerprint == "fp_x"

    def test_sync_detects_no_change(self, tmp_path: Path) -> None:
        backend = self._make_backend(tmp_path)
        state = StateSnapshot(environment="dev")
        backend.save("dev", state)

        # Sync should not need to pull (already up to date)
        assert not backend._needs_sync("dev")

    def test_sync_detects_remote_change(self, tmp_path: Path) -> None:
        """When another instance pushes, the first detects staleness."""
        backend1 = self._make_backend(tmp_path)
        state = StateSnapshot(environment="dev")
        backend1.save("dev", state)

        # Simulate another instance pushing a change
        from assets.state.tiered import TieredBackend

        remote_dir = tmp_path / "remote"
        local_dir2 = tmp_path / "local2"
        backend2 = TieredBackend(str(remote_dir), local_path=local_dir2)
        backend2.pull("dev")
        state2 = StateSnapshot(
            environment="dev",
            assets={"new": AssetState(id="new", fingerprint="fp_new")},
        )
        backend2.save("dev", state2)

        # backend1 should detect the change
        backend1._synced["dev"] = False
        assert backend1._needs_sync("dev")

    def test_list_environments(self, tmp_path: Path) -> None:
        backend = self._make_backend(tmp_path)
        backend.save("dev", StateSnapshot(environment="dev"))
        backend.save("prod", StateSnapshot(environment="prod"))
        envs = backend.list_environments()
        assert set(envs) == {"dev", "prod"}

    def test_list_environments_ignores_lock_files(self, tmp_path: Path) -> None:
        """list_environments() skips .lock files in remote."""
        backend = self._make_backend(tmp_path)
        backend.save("dev", StateSnapshot(environment="dev"))

        # Create a lock file in remote root
        remote_dir = tmp_path / "remote"
        (remote_dir / "dev.lock").write_text("token")

        envs = backend.list_environments()
        assert envs == ["dev"]

    def test_delete_environment(self, tmp_path: Path) -> None:
        backend = self._make_backend(tmp_path)
        backend.save("dev", StateSnapshot(environment="dev"))
        backend.delete_environment("dev")
        assert backend.load("dev") is None

    def test_delete_environment_removes_remote_dir(self, tmp_path: Path) -> None:
        """delete_environment() removes the remote env subdirectory."""
        backend = self._make_backend(tmp_path)
        backend.save("dev", StateSnapshot(environment="dev"))

        remote_env_dir = tmp_path / "remote" / "dev"
        assert remote_env_dir.exists()

        backend.delete_environment("dev")
        assert not remote_env_dir.exists()

    def test_lock(self, tmp_path: Path) -> None:
        backend = self._make_backend(tmp_path)
        with backend.lock("dev"):
            pass  # should succeed

    def test_lock_creates_remote_lock_file(self, tmp_path: Path) -> None:
        backend = self._make_backend(tmp_path)
        remote_dir = tmp_path / "remote"
        lock_file = remote_dir / "dev.lock"

        with backend.lock("dev"):
            assert lock_file.exists()
        # Lock file should be cleaned up
        assert not lock_file.exists()

    def test_snapshot_version_increments(self, tmp_path: Path) -> None:
        backend = self._make_backend(tmp_path)
        backend.save("dev", StateSnapshot(environment="dev"))

        snap1 = json.loads((tmp_path / "remote" / "dev" / "snapshot.json").read_text())
        assert snap1["version"] == 1

        backend.save("dev", StateSnapshot(environment="dev"))
        snap2 = json.loads((tmp_path / "remote" / "dev" / "snapshot.json").read_text())
        assert snap2["version"] == 2

    def test_close_cleans_up(self, tmp_path: Path) -> None:
        backend = self._make_backend(tmp_path)
        backend.save("dev", StateSnapshot(environment="dev"))
        backend.close()
        assert getattr(backend, "_local")._connections == {}

    def test_double_lock_raises(self, tmp_path: Path) -> None:
        backend = self._make_backend(tmp_path)
        with backend.lock("dev"):
            with pytest.raises(RuntimeError, match="already locked"):
                with backend.lock("dev"):
                    pass

    def test_lock_different_environments(self, tmp_path: Path) -> None:
        backend = self._make_backend(tmp_path)
        with backend.lock("dev"):
            # Locking a different environment should work
            with backend.lock("staging"):
                pass

    def test_lock_released_after_exception(self, tmp_path: Path) -> None:
        backend = self._make_backend(tmp_path)
        with pytest.raises(ValueError, match="test"):
            with backend.lock("dev"):
                raise ValueError("test")
        # Lock should be released — can lock again
        with backend.lock("dev"):
            pass

    def test_context_manager(self, tmp_path: Path) -> None:
        from assets.state.tiered import TieredBackend

        remote_dir = tmp_path / "remote"
        remote_dir.mkdir()
        local_dir = tmp_path / "local"
        with TieredBackend(str(remote_dir), local_path=local_dir) as backend:
            backend.save("dev", StateSnapshot(environment="dev"))
            loaded = backend.load("dev")
            assert loaded is not None
        # Connections should be closed after exit
        assert getattr(backend, "_local")._connections == {}

    def test_remote_snapshot_missing_after_exists_returns_none(
        self,
        tmp_path: Path,
    ) -> None:
        backend = self._make_backend(tmp_path)
        with patch.object(backend._fs, "exists", return_value=True):
            with patch.object(backend._fs, "cat_file", side_effect=FileNotFoundError):
                assert backend._remote_snapshot("dev") is None

    def test_remote_snapshot_unexpected_error_returns_none(
        self, tmp_path: Path
    ) -> None:
        backend = self._make_backend(tmp_path)
        with patch.object(backend._fs, "exists", return_value=True):
            with patch.object(backend._fs, "cat_file", side_effect=RuntimeError):
                assert backend._remote_snapshot("dev") is None

    def test_pull_without_remote_db_is_noop(self, tmp_path: Path) -> None:
        backend = self._make_backend(tmp_path)
        local_db = backend._local_db_path("dev")
        assert not local_db.exists()

        backend.pull("dev")

        assert not local_db.exists()

    def test_pull_race_file_not_found_is_swallowed(self, tmp_path: Path) -> None:
        backend = self._make_backend(tmp_path)

        with patch.object(backend._fs, "exists", return_value=True):
            with patch.object(backend._fs, "get_file", side_effect=FileNotFoundError):
                backend.pull("dev")

        assert not backend._local_db_path("dev").exists()

    def test_sync_resets_synced_before_ensure(self, tmp_path: Path) -> None:
        backend = self._make_backend(tmp_path)
        backend._synced["dev"] = True

        with patch.object(backend, "_ensure_synced") as ensure_synced:
            backend.sync("dev")

        ensure_synced.assert_called_once_with("dev")
        assert backend._synced.get("dev") is not True

    def test_sync_all_environments(self, tmp_path: Path) -> None:
        """sync(None) syncs all known environments."""
        backend = self._make_backend(tmp_path)
        backend.save("dev", StateSnapshot(environment="dev"))
        backend.save("prod", StateSnapshot(environment="prod"))

        # Mark both as synced
        backend._synced["dev"] = True
        backend._synced["prod"] = True

        # sync() with no arg syncs all
        backend.sync()

        # After sync, both should be re-synced
        assert backend._synced.get("dev") is True
        assert backend._synced.get("prod") is True

    def test_per_env_sync_tracking(self, tmp_path: Path) -> None:
        """_synced is per-environment; syncing one doesn't affect others."""
        backend = self._make_backend(tmp_path)
        backend.save("dev", StateSnapshot(environment="dev"))
        backend.save("prod", StateSnapshot(environment="prod"))

        # Reset only dev sync state
        backend._synced.pop("dev", None)
        prod_synced_before = backend._synced.get("prod")

        backend.sync("dev")

        # prod sync state unchanged
        assert backend._synced.get("prod") == prod_synced_before

    def test_copy_environment(self, tmp_path: Path) -> None:
        """copy_environment() copies state to a new env and pushes."""
        backend = self._make_backend(tmp_path)
        state = StateSnapshot(
            environment="dev",
            assets={"a": AssetState(id="a", fingerprint="fp1")},
        )
        backend.save("dev", state)

        backend.copy_environment("dev", "staging")

        loaded = backend.load("staging")
        assert loaded is not None
        assert "a" in loaded.assets
        assert loaded.assets["a"].fingerprint == "fp1"

    def test_copy_environment_pushes_to_remote(self, tmp_path: Path) -> None:
        """copy_environment() pushes the target to remote."""
        backend = self._make_backend(tmp_path)
        state = StateSnapshot(
            environment="dev",
            assets={"a": AssetState(id="a", fingerprint="fp1")},
        )
        backend.save("dev", state)

        backend.copy_environment("dev", "staging")

        # Verify remote files exist for staging
        remote_dir = tmp_path / "remote"
        assert (remote_dir / "staging" / "state.db").exists()
        assert (remote_dir / "staging" / "snapshot.json").exists()

    def test_copy_environment_independent_of_source(self, tmp_path: Path) -> None:
        """Changes to copied env don't affect the source."""
        backend = self._make_backend(tmp_path)
        state = StateSnapshot(
            environment="dev",
            assets={"a": AssetState(id="a", fingerprint="fp1")},
        )
        backend.save("dev", state)
        backend.copy_environment("dev", "staging")

        # Modify staging
        staging_state = StateSnapshot(
            environment="staging",
            assets={"b": AssetState(id="b", fingerprint="fp2")},
        )
        backend.save("staging", staging_state)

        # Source should be unchanged
        dev_loaded = backend.load("dev")
        assert dev_loaded is not None
        assert "a" in dev_loaded.assets
        assert "b" not in dev_loaded.assets

    def test_copy_environment_visible_to_other_instances(self, tmp_path: Path) -> None:
        """A second instance can load the copied environment from remote."""
        from assets.state.tiered import TieredBackend

        backend1 = self._make_backend(tmp_path)
        state = StateSnapshot(
            environment="dev",
            assets={"a": AssetState(id="a", fingerprint="fp1")},
        )
        backend1.save("dev", state)
        backend1.copy_environment("dev", "staging")

        # Second instance
        remote_dir = tmp_path / "remote"
        local_dir2 = tmp_path / "local2"
        backend2 = TieredBackend(str(remote_dir), local_path=local_dir2)

        loaded = backend2.load("staging")
        assert loaded is not None
        assert "a" in loaded.assets


# ─── TieredBackend locking edge cases ──────────────────────


class TestTieredBackendLockingEdgeCases:
    """Edge cases for the remote locking protocol.

    All tests use real filesystem operations — no monkey-patching of
    fsspec methods.  Two TieredBackend instances pointing at the same
    remote directory simulate multi-process contention.
    """

    @staticmethod
    def _make_backend(
        tmp_path: Path,
        *,
        remote_dir: Path | None = None,
        label: str = "local",
        lock_timeout: int = 300,
        lock_retries: int = 10,
    ) -> Any:
        from assets.state.tiered import TieredBackend

        if remote_dir is None:
            remote_dir = tmp_path / "remote"
        remote_dir.mkdir(exist_ok=True)
        local_dir = tmp_path / label
        return TieredBackend(
            str(remote_dir),
            local_path=local_dir,
            lock_timeout=lock_timeout,
            lock_retries=lock_retries,
        )

    def test_lock_held_then_released_allows_retry(self, tmp_path: Path) -> None:
        """When another backend holds the lock, we back off and retry.

        Backend B acquires the lock first.  Backend A tries, sees the
        existing lock, backs off.  A background thread releases B's
        lock after a short delay, and A succeeds on retry.
        """
        import threading

        remote_dir = tmp_path / "remote"
        backend_a = self._make_backend(
            tmp_path,
            remote_dir=remote_dir,
            label="local_a",
            lock_retries=10,
            lock_timeout=300,
        )
        backend_b = self._make_backend(
            tmp_path,
            remote_dir=remote_dir,
            label="local_b",
            lock_retries=10,
            lock_timeout=300,
        )

        # B acquires the lock (writes lock file with its own token)
        backend_b._acquire_remote_lock("dev")

        def release_b_after_delay() -> None:
            time.sleep(0.3)
            backend_b._release_remote_lock("dev")

        t = threading.Thread(target=release_b_after_delay, daemon=True)
        t.start()

        # A should retry until B releases, then succeed
        with backend_a.lock("dev"):
            assert "dev" in backend_a._remote_locks

        t.join(timeout=2)
        assert "dev" not in backend_a._remote_locks

    def test_persistent_lock_raises_after_retries(self, tmp_path: Path) -> None:
        """When a non-stale lock is never released, RuntimeError after retries."""
        remote_dir = tmp_path / "remote"
        backend_a = self._make_backend(
            tmp_path,
            remote_dir=remote_dir,
            label="local_a",
            lock_retries=2,
            lock_timeout=300,
        )
        backend_b = self._make_backend(
            tmp_path,
            remote_dir=remote_dir,
            label="local_b",
            lock_retries=2,
            lock_timeout=300,
        )

        # B holds the lock — never releases
        backend_b._acquire_remote_lock("dev")

        with pytest.raises(RuntimeError, match="Could not acquire remote lock"):
            with backend_a.lock("dev"):
                pass

        # Cleanup
        backend_b._release_remote_lock("dev")

    def test_stale_lock_detected_and_removed(self, tmp_path: Path) -> None:
        """A lock file older than lock_timeout is detected as stale and removed.

        Uses a real mtime backdate to trigger stale detection without
        affecting our own newly-written lock files.
        """
        backend = self._make_backend(tmp_path, lock_timeout=5, lock_retries=5)
        lock_path = backend._remote_lock_path("dev")

        # Create a lock file held by another process
        backend._fs.mkdirs(backend._root, exist_ok=True)
        backend._fs.pipe_file(lock_path, b"stale_holder_token")

        # Backdate the file's mtime by 10 seconds (> lock_timeout of 5s)
        real_path = Path(lock_path)
        stat = real_path.stat()
        os.utime(
            real_path,
            ns=(stat.st_atime_ns, stat.st_mtime_ns - 10_000_000_000),
        )

        # Should detect stale lock, remove it, and acquire our own
        with backend.lock("dev"):
            assert "dev" in backend._remote_locks

    def test_configurable_lock_retries_honored(self, tmp_path: Path) -> None:
        """Backend with lock_retries=2 fails after exactly 2 attempts.

        A freshly-written lock file naturally has a recent mtime, so no
        mock is needed to make it appear non-stale.
        """
        remote_dir = tmp_path / "remote"
        backend_a = self._make_backend(
            tmp_path,
            remote_dir=remote_dir,
            label="local_a",
            lock_retries=2,
            lock_timeout=300,
        )
        backend_b = self._make_backend(
            tmp_path,
            remote_dir=remote_dir,
            label="local_b",
        )

        # B acquires a real lock — naturally fresh mtime
        backend_b._acquire_remote_lock("dev")

        with pytest.raises(RuntimeError, match="Could not acquire remote lock"):
            with backend_a.lock("dev"):
                pass

        # Cleanup
        backend_b._release_remote_lock("dev")

    def test_stale_locks_reappearing_terminates(self, tmp_path: Path) -> None:
        """If stale locks keep reappearing, loop terminates after retries.

        Overrides ``_try_remove_stale_lock`` in a test subclass so it
        removes the lock *and* immediately recreates a new stale one.
        This deterministically simulates another process that keeps
        crashing and leaving stale locks — no threading race.
        """
        from assets.state.tiered import TieredBackend

        class ReappearingStaleLockBackend(TieredBackend):
            """Backend where removing a stale lock causes a new one."""

            def _try_remove_stale_lock(self, lock_path: str, environment: str) -> bool:
                removed = super()._try_remove_stale_lock(lock_path, environment)
                if removed:
                    # Immediately recreate a stale lock
                    real = Path(lock_path)
                    real.write_bytes(b"reappearing_stale")
                    st = real.stat()
                    os.utime(
                        real,
                        ns=(
                            st.st_atime_ns,
                            st.st_mtime_ns - 10_000_000_000,
                        ),
                    )
                return removed

        remote_dir = tmp_path / "remote"
        remote_dir.mkdir(exist_ok=True)
        local_dir = tmp_path / "local"
        backend = ReappearingStaleLockBackend(
            str(remote_dir),
            local_path=local_dir,
            lock_timeout=1,
            lock_retries=3,
        )

        lock_path = backend._remote_lock_path("dev")
        real_lock = Path(lock_path)

        # Seed the initial stale lock
        backend._fs.mkdirs(backend._root, exist_ok=True)
        real_lock.write_bytes(b"initial_stale")
        st = real_lock.stat()
        os.utime(
            real_lock,
            ns=(st.st_atime_ns, st.st_mtime_ns - 10_000_000_000),
        )

        with pytest.raises(RuntimeError, match="Could not acquire remote lock"):
            with backend.lock("dev"):
                pass


class TestTieredBackendInternalBranches:
    @staticmethod
    def _make_backend(
        tmp_path: Path,
        *,
        lock_timeout: int = 300,
        lock_retries: int = 3,
    ) -> Any:
        from assets.state.tiered import TieredBackend

        remote_dir = tmp_path / "remote"
        remote_dir.mkdir(exist_ok=True)
        local_dir = tmp_path / "local"
        return TieredBackend(
            str(remote_dir),
            local_path=local_dir,
            lock_timeout=lock_timeout,
            lock_retries=lock_retries,
        )

    def test_acquire_lock_when_exists_raises_file_not_found(
        self, tmp_path: Path
    ) -> None:
        backend = self._make_backend(tmp_path)

        with patch.object(backend._fs, "exists", side_effect=FileNotFoundError):
            backend._acquire_remote_lock("dev")

        assert "dev" in backend._remote_locks
        backend._release_remote_lock("dev")

    def test_acquire_lock_retries_when_pipe_file_fails(self, tmp_path: Path) -> None:
        backend = self._make_backend(tmp_path, lock_retries=2)

        with patch.object(backend._fs, "exists", return_value=False):
            with patch.object(backend._fs, "pipe_file", side_effect=OSError("boom")):
                with patch("assets.state.tiered.time.sleep", return_value=None):
                    with pytest.raises(
                        RuntimeError,
                        match="Could not acquire remote lock",
                    ):
                        backend._acquire_remote_lock("dev")

    def test_acquire_lock_retries_when_verify_ownership_fails(
        self,
        tmp_path: Path,
    ) -> None:
        backend = self._make_backend(tmp_path, lock_retries=2)

        with patch.object(backend._fs, "exists", return_value=False):
            with patch.object(backend._fs, "cat_file", side_effect=OSError("boom")):
                with patch("assets.state.tiered.time.sleep", return_value=None):
                    with pytest.raises(
                        RuntimeError,
                        match="Could not acquire remote lock",
                    ):
                        backend._acquire_remote_lock("dev")

    def test_try_remove_stale_lock_info_error_returns_false(
        self,
        tmp_path: Path,
    ) -> None:
        backend = self._make_backend(tmp_path)

        with patch.object(backend._fs, "info", side_effect=RuntimeError("boom")):
            assert backend._try_remove_stale_lock("/tmp/dev.lock", "dev") is False

    def test_try_remove_stale_lock_missing_mtime_returns_false(
        self,
        tmp_path: Path,
    ) -> None:
        backend = self._make_backend(tmp_path)

        with patch.object(backend._fs, "info", return_value={}):
            assert backend._try_remove_stale_lock("/tmp/dev.lock", "dev") is False

    def test_try_remove_stale_lock_disappeared_returns_true(
        self,
        tmp_path: Path,
    ) -> None:
        backend = self._make_backend(tmp_path)

        with patch.object(backend._fs, "info", side_effect=FileNotFoundError):
            assert backend._try_remove_stale_lock("/tmp/dev.lock", "dev") is True

    def test_try_remove_stale_lock_datetime_mtime_path(self, tmp_path: Path) -> None:
        backend = self._make_backend(tmp_path)
        stale_mtime = datetime.now(timezone.utc) - timedelta(hours=1)

        with patch.object(backend._fs, "info", return_value={"mtime": stale_mtime}):
            with patch.object(backend._fs, "rm") as rm:
                assert backend._try_remove_stale_lock("/tmp/dev.lock", "dev") is True

        rm.assert_called_once_with("/tmp/dev.lock")

    def test_try_remove_stale_lock_rm_failure_returns_false(
        self,
        tmp_path: Path,
    ) -> None:
        backend = self._make_backend(tmp_path)
        stale_epoch = time.time() - 1_000

        with patch.object(backend._fs, "info", return_value={"mtime": stale_epoch}):
            with patch.object(backend._fs, "rm", side_effect=OSError("boom")):
                assert backend._try_remove_stale_lock("/tmp/dev.lock", "dev") is False


# ─── TieredBackend delegation ─────────────────────────────


class TestTieredBackendDelegation:
    """Test that TieredBackend properly delegates to local SQLite."""

    def _make_backend(self, tmp_path: Path) -> Any:
        from assets.state.tiered import TieredBackend

        remote_dir = tmp_path / "remote"
        remote_dir.mkdir()
        local_dir = tmp_path / "local"
        return TieredBackend(str(remote_dir), local_path=local_dir)

    def test_asset_version_delegation(self, tmp_path: Path) -> None:
        """asset_version() delegates to local SQLite and returns correct record."""
        backend = self._make_backend(tmp_path)

        # Save v1
        state = StateSnapshot(
            environment="dev",
            assets={"a": AssetState(id="a", fingerprint="v1", version=1)},
        )
        backend.save("dev", state)

        # Save v2
        state.assets["a"] = AssetState(
            id="a",
            fingerprint="v2",
            version=2,
            data={"sql": "SELECT 2"},
        )
        backend.save("dev", state)

        # Look up v1
        v1 = backend.asset_version("dev", "a", 1)
        assert v1 is not None
        assert v1["fingerprint"] == "v1"

        # Look up v2
        v2 = backend.asset_version("dev", "a", 2)
        assert v2 is not None
        assert v2["fingerprint"] == "v2"

        # Look up nonexistent v3
        v3 = backend.asset_version("dev", "a", 3)
        assert v3 is None

    def test_prune_history_delegates_and_pushes(self, tmp_path: Path) -> None:
        """prune_history() delegates to local, pushes if rows pruned."""
        backend = self._make_backend(tmp_path)

        # Create history entries
        state = StateSnapshot(
            environment="dev",
            assets={"a": AssetState(id="a", fingerprint="v1", version=1)},
        )
        backend.save("dev", state)

        # Record remote snapshot version after save
        snap_before = json.loads(
            (tmp_path / "remote" / "dev" / "snapshot.json").read_text()
        )

        # Backdate history so prune has something to remove
        backend._local.conn("dev").execute(
            "UPDATE assets_history SET recorded_at = '2020-01-01T00:00:00.000Z'"
        )
        backend._local.conn("dev").commit()

        # Prune
        count = backend.prune_history(keep_days=0)
        assert count > 0

        # Remote snapshot version should have incremented (push happened)
        snap_after = json.loads(
            (tmp_path / "remote" / "dev" / "snapshot.json").read_text()
        )
        assert snap_after["version"] > snap_before["version"]

    def test_prune_history_no_rows_skips_push(self, tmp_path: Path) -> None:
        """prune_history() with nothing to prune does NOT push."""
        backend = self._make_backend(tmp_path)

        # Save state so remote snapshot exists
        backend.save("dev", StateSnapshot(environment="dev"))

        snap_before = json.loads(
            (tmp_path / "remote" / "dev" / "snapshot.json").read_text()
        )

        # Prune with keep_days=9999 — nothing old enough to prune
        count = backend.prune_history(keep_days=9999)
        assert count == 0

        # Snapshot should NOT have changed (no push)
        snap_after = json.loads(
            (tmp_path / "remote" / "dev" / "snapshot.json").read_text()
        )
        assert snap_after["version"] == snap_before["version"]

    def test_asset_history_and_changelog_delegate(self, tmp_path: Path) -> None:
        backend = self._make_backend(tmp_path)

        state = StateSnapshot(
            environment="dev",
            assets={"a": AssetState(id="a", fingerprint="v1", version=1)},
        )
        backend.save("dev", state)

        state.assets["a"] = AssetState(id="a", fingerprint="v2", version=2)
        backend.save("dev", state)

        history = backend.asset_history("dev", "a")
        assert len(history) == 2
        assert history[0]["action"] == "update"

        changelog = backend.environment_changelog("dev")
        assert any(entry["asset_id"] == "a" for entry in changelog)

        future_since = (datetime.now(timezone.utc) + timedelta(days=1)).strftime(
            "%Y-%m-%dT%H:%M:%S.%fZ"
        )
        assert backend.environment_changelog("dev", since=future_since) == []

    def test_release_remote_lock_failure_is_swallowed(self, tmp_path: Path) -> None:
        backend = self._make_backend(tmp_path)
        backend._remote_locks.add("dev")

        with patch.object(backend._fs, "rm", side_effect=OSError("boom")):
            backend._release_remote_lock("dev")

        assert "dev" not in backend._remote_locks

    def test_conn_is_method_taking_environment(self, tmp_path: Path) -> None:
        """backend._local.conn(environment) is a method, not property."""
        backend = self._make_backend(tmp_path)
        backend.save("dev", StateSnapshot(environment="dev"))

        # conn is a method that takes environment name
        conn = backend._local.conn("dev")
        assert conn is not None

        row = conn.execute("SELECT * FROM state_metadata").fetchone()
        assert row is not None


# ─── Context manager tests for all backends ────────────────


class TestBackendContextManager:
    def test_memory_backend_context_manager(self) -> None:
        with SQLiteBackend.memory() as backend:
            backend.save("dev", StateSnapshot(environment="dev"))
            assert backend.load("dev") is not None

    def test_sqlite_backend_context_manager(self, tmp_path: Path) -> None:
        with SQLiteBackend(base_path=tmp_path / "state") as backend:
            backend.save("dev", StateSnapshot(environment="dev"))
            assert backend.load("dev") is not None
        assert getattr(backend, "_connections") == {}
