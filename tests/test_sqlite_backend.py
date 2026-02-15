"""Tests for SQLiteBackend — state persistence with version history.

The new SQLiteBackend stores each environment in its own SQLite database
(one ``state.db`` per environment directory).  In-memory mode uses a
separate ``:memory:`` connection per environment name.
"""

from __future__ import annotations

import json
from collections.abc import Generator
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from assets import SQLiteBackend
from assets.state.models import AssetState, DependencyState, StateSnapshot
from assets.state.sqlite import _parse_dt


# ─── Fixtures ───────────────────────────────────────────────


@pytest.fixture
def mem_backend() -> Generator[SQLiteBackend, None, None]:
    """In-memory backend — fast, no filesystem."""
    b = SQLiteBackend.memory()
    yield b
    b.close()


@pytest.fixture
def file_backend(tmp_path: Path) -> Generator[SQLiteBackend, None, None]:
    """File-backed backend — one state.db per environment directory."""
    b = SQLiteBackend(base_path=tmp_path)
    yield b
    b.close()


# ─── Basic load / save ──────────────────────────────────────


class TestSQLiteBackendBasic:
    """Core StateBackend interface tests."""

    def test_load_empty(self, mem_backend: SQLiteBackend) -> None:
        assert mem_backend.load("dev") is None

    def test_save_and_load(self, mem_backend: SQLiteBackend) -> None:
        state = StateSnapshot(environment="dev")
        mem_backend.save("dev", state)
        loaded = mem_backend.load("dev")
        assert loaded is not None
        assert loaded.environment == "dev"

    def test_save_and_load_with_assets(self, mem_backend: SQLiteBackend) -> None:
        state = StateSnapshot(
            environment="dev",
            assets={"a": AssetState(id="a", fingerprint="fp1", type="model")},
        )
        mem_backend.save("dev", state)
        loaded = mem_backend.load("dev")
        assert loaded is not None
        assert "a" in loaded.assets
        assert loaded.assets["a"].fingerprint == "fp1"
        assert loaded.assets["a"].type == "model"

    def test_save_and_load_with_dependencies(self, mem_backend: SQLiteBackend) -> None:
        state = StateSnapshot(
            environment="dev",
            dependencies=[
                DependencyState(
                    source="staging.users",
                    target="raw.users",
                    type="ref",
                    fingerprint="depfp1",
                ),
            ],
        )
        mem_backend.save("dev", state)
        loaded = mem_backend.load("dev")
        assert loaded is not None
        assert len(loaded.dependencies) == 1
        assert loaded.dependencies[0].source == "staging.users"
        assert loaded.dependencies[0].target == "raw.users"

    def test_save_and_load_with_data(self, mem_backend: SQLiteBackend) -> None:
        state = StateSnapshot(
            environment="dev",
            assets={
                "a": AssetState(
                    id="a",
                    fingerprint="fp1",
                    data={"sql": "SELECT 1", "tags": ["core"]},
                ),
            },
        )
        mem_backend.save("dev", state)
        loaded = mem_backend.load("dev")
        assert loaded is not None
        assert loaded.assets["a"].data == {"sql": "SELECT 1", "tags": ["core"]}

    def test_save_and_load_with_metadata(self, mem_backend: SQLiteBackend) -> None:
        state = StateSnapshot(
            environment="dev",
            metadata={"team": "data-eng", "version": 42},
        )
        mem_backend.save("dev", state)
        loaded = mem_backend.load("dev")
        assert loaded is not None
        assert loaded.metadata == {"team": "data-eng", "version": 42}

    def test_file_backend_save_and_load(self, file_backend: SQLiteBackend) -> None:
        state = StateSnapshot(
            environment="dev",
            assets={"x": AssetState(id="x", fingerprint="fp_x")},
        )
        file_backend.save("dev", state)
        loaded = file_backend.load("dev")
        assert loaded is not None
        assert "x" in loaded.assets

    def test_repeated_saves_keep_single_metadata_row(
        self, mem_backend: SQLiteBackend
    ) -> None:
        """Regression: state_metadata must be a singleton row.

        Previously the table had no primary key, so every save()
        inserted a new row.  load() used fetchone() and returned the
        oldest (stale) metadata.
        """
        state = StateSnapshot(environment="dev", version=1)
        mem_backend.save("dev", state)
        state.version = 2
        mem_backend.save("dev", state)
        state.version = 3
        mem_backend.save("dev", state)

        conn = mem_backend.conn("dev")
        rows = conn.execute("SELECT * FROM state_metadata").fetchall()
        assert len(rows) == 1, f"Expected exactly 1 metadata row, got {len(rows)}"

        loaded = mem_backend.load("dev")
        assert loaded is not None
        assert loaded.version == 3

    def test_repeated_saves_metadata_on_file_backend(
        self, file_backend: SQLiteBackend
    ) -> None:
        """Same singleton-metadata check for file-backed backend."""
        state = StateSnapshot(environment="dev", version=1)
        file_backend.save("dev", state)
        state.version = 5
        file_backend.save("dev", state)

        conn = file_backend.conn("dev")
        rows = conn.execute("SELECT * FROM state_metadata").fetchall()
        assert len(rows) == 1

        loaded = file_backend.load("dev")
        assert loaded is not None
        assert loaded.version == 5


# ─── List environments ──────────────────────────────────────


class TestListEnvironments:
    """list_environments() scans directories (file) or connections (memory)."""

    def test_list_environments_memory(self, mem_backend: SQLiteBackend) -> None:
        mem_backend.save("dev", StateSnapshot(environment="dev"))
        mem_backend.save("prod", StateSnapshot(environment="prod"))
        envs = mem_backend.list_environments()
        assert set(envs) == {"dev", "prod"}

    def test_list_environments_file(self, file_backend: SQLiteBackend) -> None:
        file_backend.save("dev", StateSnapshot(environment="dev"))
        file_backend.save("staging", StateSnapshot(environment="staging"))
        envs = file_backend.list_environments()
        assert set(envs) == {"dev", "staging"}

    def test_list_environments_empty(self, mem_backend: SQLiteBackend) -> None:
        assert mem_backend.list_environments() == []

    def test_list_environments_memory_ignores_unsaved_connections(
        self, mem_backend: SQLiteBackend
    ) -> None:
        """A connection opened but never saved should not appear."""
        # Opening the connection but not saving — no state_metadata row.
        _ = mem_backend.conn("ghost")
        assert "ghost" not in mem_backend.list_environments()


# ─── Delete environment ─────────────────────────────────────


class TestDeleteEnvironment:
    """delete_environment() removes directory (file) or discards connection (memory)."""

    def test_delete_memory(self, mem_backend: SQLiteBackend) -> None:
        mem_backend.save("dev", StateSnapshot(environment="dev"))
        mem_backend.delete_environment("dev")
        assert mem_backend.load("dev") is None
        assert mem_backend.list_environments() == []

    def test_delete_file(self, file_backend: SQLiteBackend) -> None:
        file_backend.save("dev", StateSnapshot(environment="dev"))
        env_dir = file_backend.env_db_path("dev").parent
        assert env_dir.exists()
        file_backend.delete_environment("dev")
        assert not env_dir.exists()
        assert file_backend.list_environments() == []

    def test_delete_one_keeps_others(self, mem_backend: SQLiteBackend) -> None:
        mem_backend.save("dev", StateSnapshot(environment="dev"))
        mem_backend.save("prod", StateSnapshot(environment="prod"))
        mem_backend.delete_environment("dev")
        assert mem_backend.load("dev") is None
        assert mem_backend.load("prod") is not None

    def test_delete_nonexistent_environment(self, mem_backend: SQLiteBackend) -> None:
        """Deleting an environment that was never created should not raise."""
        mem_backend.delete_environment("nope")  # no error


# ─── Close environment connection ───────────────────────────


class TestCloseEnv:
    """close_env() closes a single environment connection."""

    def test_close_env_and_reopen(self, mem_backend: SQLiteBackend) -> None:
        """After close_env, accessing the env again creates a fresh connection."""
        mem_backend.save("dev", StateSnapshot(environment="dev"))
        mem_backend.close_env("dev")
        # In-memory: connection is gone, so load returns None
        assert mem_backend.load("dev") is None

    def test_close_env_file_backend_reopen(self, file_backend: SQLiteBackend) -> None:
        """File-backed: close_env then re-access reconnects to same DB file."""
        state = StateSnapshot(
            environment="dev",
            assets={"a": AssetState(id="a", fingerprint="fp1")},
        )
        file_backend.save("dev", state)
        file_backend.close_env("dev")
        # Reconnects on next access — data persists on disk
        loaded = file_backend.load("dev")
        assert loaded is not None
        assert "a" in loaded.assets

    def test_close_env_nonexistent(self, mem_backend: SQLiteBackend) -> None:
        """close_env on an unknown environment should not raise."""
        mem_backend.close_env("nope")  # no error


# ─── Copy environment ───────────────────────────────────────


class TestCopyEnvironment:
    """copy_environment() duplicates state from source to target."""

    def test_copy_memory(self, mem_backend: SQLiteBackend) -> None:
        state = StateSnapshot(
            environment="dev",
            assets={"a": AssetState(id="a", fingerprint="fp1", type="model")},
            dependencies=[
                DependencyState(source="a", target="b", fingerprint="depfp1"),
            ],
            metadata={"owner": "alice"},
        )
        mem_backend.save("dev", state)
        mem_backend.copy_environment("dev", "staging")

        copied = mem_backend.load("staging")
        assert copied is not None
        assert "a" in copied.assets
        assert copied.assets["a"].fingerprint == "fp1"
        assert len(copied.dependencies) == 1
        assert copied.metadata == {"owner": "alice"}

    def test_copy_file_backend(self, file_backend: SQLiteBackend) -> None:
        state = StateSnapshot(
            environment="dev",
            assets={"x": AssetState(id="x", fingerprint="fpx")},
        )
        file_backend.save("dev", state)
        file_backend.copy_environment("dev", "prod")

        copied = file_backend.load("prod")
        assert copied is not None
        assert "x" in copied.assets
        assert file_backend.env_db_path("prod").exists()

    def test_copy_nonexistent_source(self, mem_backend: SQLiteBackend) -> None:
        """Copying from a non-existent source should be a no-op."""
        mem_backend.copy_environment("missing", "target")
        assert mem_backend.load("target") is None

    def test_copy_overwrites_target(self, mem_backend: SQLiteBackend) -> None:
        mem_backend.save(
            "dev",
            StateSnapshot(
                environment="dev",
                assets={"a": AssetState(id="a", fingerprint="new")},
            ),
        )
        mem_backend.save(
            "staging",
            StateSnapshot(
                environment="staging",
                assets={"old": AssetState(id="old", fingerprint="old_fp")},
            ),
        )
        mem_backend.copy_environment("dev", "staging")

        copied = mem_backend.load("staging")
        assert copied is not None
        assert "a" in copied.assets
        # The old asset should be gone (replaced by source state)
        assert "old" not in copied.assets

    def test_copy_isolation(self, mem_backend: SQLiteBackend) -> None:
        """After copying, changes to source do not affect target."""
        mem_backend.save(
            "dev",
            StateSnapshot(
                environment="dev",
                assets={"a": AssetState(id="a", fingerprint="v1")},
            ),
        )
        mem_backend.copy_environment("dev", "staging")

        # Mutate source
        mem_backend.save(
            "dev",
            StateSnapshot(
                environment="dev",
                assets={"a": AssetState(id="a", fingerprint="v2")},
            ),
        )

        staging = mem_backend.load("staging")
        assert staging is not None
        assert staging.assets["a"].fingerprint == "v1"


# ─── Update (overwrite) state ───────────────────────────────


class TestSQLiteBackendUpdate:
    """Tests for updating (overwriting) state."""

    def test_update_asset(self, mem_backend: SQLiteBackend) -> None:
        state = StateSnapshot(
            environment="dev",
            assets={"a": AssetState(id="a", fingerprint="fp1")},
        )
        mem_backend.save("dev", state)

        state.assets["a"] = AssetState(id="a", fingerprint="fp2", type="updated")
        mem_backend.save("dev", state)

        loaded = mem_backend.load("dev")
        assert loaded is not None
        assert loaded.assets["a"].fingerprint == "fp2"
        assert loaded.assets["a"].type == "updated"

    def test_add_asset(self, mem_backend: SQLiteBackend) -> None:
        state = StateSnapshot(
            environment="dev",
            assets={"a": AssetState(id="a", fingerprint="fp1")},
        )
        mem_backend.save("dev", state)

        state.assets["b"] = AssetState(id="b", fingerprint="fp_b")
        mem_backend.save("dev", state)

        loaded = mem_backend.load("dev")
        assert loaded is not None
        assert len(loaded.assets) == 2

    def test_remove_asset(self, mem_backend: SQLiteBackend) -> None:
        state = StateSnapshot(
            environment="dev",
            assets={
                "a": AssetState(id="a", fingerprint="fp1"),
                "b": AssetState(id="b", fingerprint="fp2"),
            },
        )
        mem_backend.save("dev", state)

        del state.assets["b"]
        mem_backend.save("dev", state)

        loaded = mem_backend.load("dev")
        assert loaded is not None
        assert len(loaded.assets) == 1
        assert "a" in loaded.assets


# ─── Environment isolation ──────────────────────────────────


class TestSQLiteBackendIsolation:
    """Tests for environment isolation (each env = separate DB)."""

    def test_environments_isolated(self, mem_backend: SQLiteBackend) -> None:
        mem_backend.save(
            "dev",
            StateSnapshot(
                environment="dev",
                assets={"a": AssetState(id="a", fingerprint="dev_fp")},
            ),
        )
        mem_backend.save(
            "prod",
            StateSnapshot(
                environment="prod",
                assets={"b": AssetState(id="b", fingerprint="prod_fp")},
            ),
        )

        dev = mem_backend.load("dev")
        prod = mem_backend.load("prod")
        assert dev is not None and prod is not None
        assert "a" in dev.assets and "b" not in dev.assets
        assert "b" in prod.assets and "a" not in prod.assets

    def test_same_asset_id_different_envs(self, mem_backend: SQLiteBackend) -> None:
        """Same asset id in two envs should not collide (separate DBs)."""
        mem_backend.save(
            "dev",
            StateSnapshot(
                environment="dev",
                assets={"shared": AssetState(id="shared", fingerprint="dev")},
            ),
        )
        mem_backend.save(
            "prod",
            StateSnapshot(
                environment="prod",
                assets={"shared": AssetState(id="shared", fingerprint="prod")},
            ),
        )

        dev = mem_backend.load("dev")
        prod = mem_backend.load("prod")
        assert dev is not None and prod is not None
        assert dev.assets["shared"].fingerprint == "dev"
        assert prod.assets["shared"].fingerprint == "prod"


# ─── Locking ────────────────────────────────────────────────


class TestSQLiteBackendLocking:
    """In-process locking tests."""

    def test_lock(self, mem_backend: SQLiteBackend) -> None:
        with mem_backend.lock("dev"):
            pass  # should succeed

    def test_double_lock_raises(self, mem_backend: SQLiteBackend) -> None:
        with mem_backend.lock("dev"):
            with pytest.raises(RuntimeError, match="already locked"):
                with mem_backend.lock("dev"):
                    pass

    def test_lock_released_after_exception(self, mem_backend: SQLiteBackend) -> None:
        with pytest.raises(ValueError):
            with mem_backend.lock("dev"):
                raise ValueError("test")
        # Lock should be released
        with mem_backend.lock("dev"):
            pass

    def test_lock_per_environment(self, mem_backend: SQLiteBackend) -> None:
        """Locks are per-environment — locking 'dev' doesn't block 'prod'."""
        with mem_backend.lock("dev"):
            with mem_backend.lock("prod"):
                pass  # should succeed


# ─── History ────────────────────────────────────────────────


class TestSQLiteBackendHistory:
    """Tests for automatic version history via triggers."""

    def test_insert_creates_history(self, mem_backend: SQLiteBackend) -> None:
        state = StateSnapshot(
            environment="dev",
            assets={"a": AssetState(id="a", fingerprint="fp1")},
        )
        mem_backend.save("dev", state)

        history = mem_backend.asset_history("dev", "a")
        assert len(history) == 1
        assert history[0]["action"] == "create"
        assert history[0]["fingerprint"] == "fp1"

    def test_update_appends_history(self, mem_backend: SQLiteBackend) -> None:
        state = StateSnapshot(
            environment="dev",
            assets={"a": AssetState(id="a", fingerprint="fp1")},
        )
        mem_backend.save("dev", state)

        state.assets["a"] = AssetState(id="a", fingerprint="fp2")
        mem_backend.save("dev", state)

        history = mem_backend.asset_history("dev", "a")
        assert len(history) == 2
        # Most recent first (ORDER BY version DESC)
        assert history[0]["action"] == "update"
        assert history[0]["fingerprint"] == "fp2"
        assert history[1]["action"] == "create"
        assert history[1]["fingerprint"] == "fp1"

    def test_delete_appends_history(self, mem_backend: SQLiteBackend) -> None:
        state = StateSnapshot(
            environment="dev",
            assets={"a": AssetState(id="a", fingerprint="fp1")},
        )
        mem_backend.save("dev", state)

        del state.assets["a"]
        mem_backend.save("dev", state)

        history = mem_backend.asset_history("dev", "a")
        assert len(history) == 2
        assert history[0]["action"] == "delete"
        assert history[1]["action"] == "create"

    def test_multiple_updates_full_history(self, mem_backend: SQLiteBackend) -> None:
        state = StateSnapshot(
            environment="dev",
            assets={"a": AssetState(id="a", fingerprint="v1", version=1)},
        )
        mem_backend.save("dev", state)

        for i in range(2, 6):
            state.assets["a"] = AssetState(id="a", fingerprint=f"v{i}", version=i)
            mem_backend.save("dev", state)

        history = mem_backend.asset_history("dev", "a")
        assert len(history) == 5
        assert history[0]["version"] == 5
        assert history[4]["version"] == 1

    def test_environment_changelog(self, mem_backend: SQLiteBackend) -> None:
        state = StateSnapshot(
            environment="dev",
            assets={
                "a": AssetState(id="a", fingerprint="fp1"),
                "b": AssetState(id="b", fingerprint="fp2"),
            },
        )
        mem_backend.save("dev", state)

        changelog = mem_backend.environment_changelog("dev")
        assert len(changelog) == 2
        names = {e["asset_id"] for e in changelog}
        assert names == {"a", "b"}

    def test_asset_version_lookup(self, mem_backend: SQLiteBackend) -> None:
        state = StateSnapshot(
            environment="dev",
            assets={"a": AssetState(id="a", fingerprint="v1", version=1)},
        )
        mem_backend.save("dev", state)

        state.assets["a"] = AssetState(
            id="a",
            fingerprint="v2",
            version=2,
            data={"sql": "SELECT 2"},
        )
        mem_backend.save("dev", state)

        v1 = mem_backend.asset_version("dev", "a", 1)
        assert v1 is not None
        assert v1["fingerprint"] == "v1"

        v2 = mem_backend.asset_version("dev", "a", 2)
        assert v2 is not None
        assert v2["fingerprint"] == "v2"

        v3 = mem_backend.asset_version("dev", "a", 3)
        assert v3 is None

    def test_history_limit(self, mem_backend: SQLiteBackend) -> None:
        state = StateSnapshot(
            environment="dev",
            assets={"a": AssetState(id="a", fingerprint="v1", version=1)},
        )
        mem_backend.save("dev", state)

        for i in range(2, 12):
            state.assets["a"] = AssetState(id="a", fingerprint=f"v{i}", version=i)
            mem_backend.save("dev", state)

        history = mem_backend.asset_history("dev", "a", limit=3)
        assert len(history) == 3
        assert history[0]["version"] == 11

    def test_history_isolated_per_environment(self, mem_backend: SQLiteBackend) -> None:
        """Each env has its own DB, so histories never cross-contaminate."""
        mem_backend.save(
            "dev",
            StateSnapshot(
                environment="dev",
                assets={"a": AssetState(id="a", fingerprint="dev_fp")},
            ),
        )
        mem_backend.save(
            "prod",
            StateSnapshot(
                environment="prod",
                assets={"a": AssetState(id="a", fingerprint="prod_fp")},
            ),
        )

        dev_history = mem_backend.asset_history("dev", "a")
        prod_history = mem_backend.asset_history("prod", "a")
        assert len(dev_history) == 1
        assert len(prod_history) == 1
        assert dev_history[0]["fingerprint"] == "dev_fp"
        assert prod_history[0]["fingerprint"] == "prod_fp"

    def test_history_data_preserved(self, mem_backend: SQLiteBackend) -> None:
        state = StateSnapshot(
            environment="dev",
            assets={
                "a": AssetState(
                    id="a",
                    fingerprint="fp1",
                    data={"sql": "SELECT 1", "columns": ["id", "name"]},
                    applied_by="alice",
                ),
            },
        )
        mem_backend.save("dev", state)

        history = mem_backend.asset_history("dev", "a")
        assert len(history) == 1
        assert history[0]["applied_by"] == "alice"
        data = json.loads(history[0]["data"])
        assert data["sql"] == "SELECT 1"

    def test_environment_changelog_since_filters(
        self, mem_backend: SQLiteBackend
    ) -> None:
        state = StateSnapshot(
            environment="dev",
            assets={"a": AssetState(id="a", fingerprint="fp1")},
        )
        mem_backend.save("dev", state)

        all_rows = mem_backend.environment_changelog(
            "dev",
            since="1970-01-01T00:00:00.000Z",
        )
        assert len(all_rows) >= 1

        future_since = (datetime.now(timezone.utc) + timedelta(days=1)).strftime(
            "%Y-%m-%dT%H:%M:%S.%fZ"
        )
        assert mem_backend.environment_changelog("dev", since=future_since) == []


# ─── Datetime parsing ───────────────────────────────────────


class TestDatetimeParsing:
    def test_parse_dt_fallback_isoformat_with_offset(self) -> None:
        parsed = _parse_dt("2026-01-01T10:20:30+00:00")
        assert parsed.tzinfo is not None
        assert parsed.year == 2026


# ─── Prune history ──────────────────────────────────────────


class TestSQLiteBackendPruneHistory:
    """Test prune_history on SQLiteBackend directly."""

    def test_prune_removes_old_entries(self, tmp_path: Path) -> None:
        backend = SQLiteBackend(base_path=tmp_path)
        state = StateSnapshot(
            environment="dev",
            assets={"a": AssetState(id="a", fingerprint="v1", version=1)},
        )
        backend.save("dev", state)

        # Backdate all history in the dev environment's DB
        backend.conn("dev").execute(
            "UPDATE assets_history SET recorded_at = '2020-01-01T00:00:00.000Z'"
        )
        backend.conn("dev").commit()

        count = backend.prune_history(keep_days=0)
        assert count >= 1

        # Verify history is gone
        history = backend.asset_history("dev", "a")
        assert len(history) == 0
        backend.close()

    def test_prune_keeps_recent_entries(self, tmp_path: Path) -> None:
        backend = SQLiteBackend(base_path=tmp_path)
        state = StateSnapshot(
            environment="dev",
            assets={"a": AssetState(id="a", fingerprint="v1", version=1)},
        )
        backend.save("dev", state)

        # History was just created — should survive prune with keep_days=90
        count = backend.prune_history(keep_days=90)
        assert count == 0

        history = backend.asset_history("dev", "a")
        assert len(history) == 1
        backend.close()


# ─── Dependency history triggers ────────────────────────────


class TestDependencyHistoryTriggers:
    """Verify triggers populate dependencies_history table."""

    def test_insert_trigger_creates_history(self, tmp_path: Path) -> None:
        backend = SQLiteBackend(base_path=tmp_path)
        state = StateSnapshot(
            environment="dev",
            dependencies=[
                DependencyState(
                    source="staging.users",
                    target="raw.users",
                    type="ref",
                    fingerprint="depfp1",
                ),
            ],
        )
        backend.save("dev", state)

        rows = (
            backend.conn("dev").execute("SELECT * FROM dependencies_history").fetchall()
        )
        assert len(rows) == 1
        assert rows[0]["action"] == "create"
        assert rows[0]["source"] == "staging.users"
        assert rows[0]["target"] == "raw.users"
        backend.close()

    def test_delete_trigger_creates_history(self, tmp_path: Path) -> None:
        backend = SQLiteBackend(base_path=tmp_path)

        state = StateSnapshot(
            environment="dev",
            dependencies=[
                DependencyState(
                    source="staging.users",
                    target="raw.users",
                    fingerprint="depfp1",
                ),
            ],
        )
        backend.save("dev", state)

        # Remove dependency
        state.dependencies = []
        backend.save("dev", state)

        rows = (
            backend.conn("dev")
            .execute("SELECT * FROM dependencies_history ORDER BY id")
            .fetchall()
        )
        # First: create, second: delete
        assert len(rows) == 2
        assert rows[0]["action"] == "create"
        assert rows[1]["action"] == "delete"
        assert rows[1]["source"] == "staging.users"
        backend.close()

    def test_update_trigger_creates_history(self, tmp_path: Path) -> None:
        """Updating a dependency's fingerprint fires the update trigger.

        Note: SQLiteBackend.save() deletes all deps then re-inserts.
        So an "update" in practice is delete+insert, yielding delete+create
        history entries. This test verifies that dep changes are tracked.
        """
        backend = SQLiteBackend(base_path=tmp_path)

        state = StateSnapshot(
            environment="dev",
            dependencies=[
                DependencyState(
                    source="staging.users",
                    target="raw.users",
                    fingerprint="depfp1",
                ),
            ],
        )
        backend.save("dev", state)

        # "Update" — save with new fingerprint (same source/target)
        state.dependencies = [
            DependencyState(
                source="staging.users",
                target="raw.users",
                fingerprint="depfp2",
            ),
        ]
        backend.save("dev", state)

        rows = (
            backend.conn("dev")
            .execute("SELECT * FROM dependencies_history ORDER BY id")
            .fetchall()
        )
        # create from first save, delete+create from second save
        assert len(rows) >= 2
        actions = [r["action"] for r in rows]
        assert "create" in actions
        backend.close()


# ─── File-backed specific tests ─────────────────────────────


class TestFileBackendPaths:
    """Tests specific to file-backed layout."""

    def test_env_db_path(self, file_backend: SQLiteBackend) -> None:
        p = file_backend.env_db_path("production")
        assert p.name == "state.db"
        assert p.parent.name == "production"

    def test_env_db_path_raises_for_memory(self, mem_backend: SQLiteBackend) -> None:
        with pytest.raises(RuntimeError, match="In-memory"):
            mem_backend.env_db_path("dev")

    def test_local_path_none_for_memory(self, mem_backend: SQLiteBackend) -> None:
        assert mem_backend.local_path is None

    def test_local_path_for_file(
        self, file_backend: SQLiteBackend, tmp_path: Path
    ) -> None:
        assert file_backend.local_path == tmp_path

    def test_state_db_created_on_save(self, file_backend: SQLiteBackend) -> None:
        file_backend.save("myenv", StateSnapshot(environment="myenv"))
        assert file_backend.env_db_path("myenv").exists()


# ─── Context manager ────────────────────────────────────────


class TestContextManager:
    """Backend supports `with` statement for clean resource management."""

    def test_context_manager(self, tmp_path: Path) -> None:
        with SQLiteBackend(base_path=tmp_path) as backend:
            backend.save("dev", StateSnapshot(environment="dev"))
            loaded = backend.load("dev")
            assert loaded is not None
