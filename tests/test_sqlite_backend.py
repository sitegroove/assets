"""Tests for SQLiteBackend — state persistence with version history."""

from pathlib import Path

import pytest

from assets import SQLiteBackend
from assets.state.models import AssetState, DependencyState, SourceFileRef, StateSnapshot


@pytest.fixture
def backend(tmp_path: Path) -> SQLiteBackend:
    b = SQLiteBackend(db_path=tmp_path / "state.db")
    yield b
    b.close()


class TestSQLiteBackendBasic:
    """Core StateBackend interface tests (mirrors test_state_backends.py)."""

    def test_load_empty(self, backend: SQLiteBackend):
        assert backend.load("dev") is None

    def test_save_and_load(self, backend: SQLiteBackend):
        state = StateSnapshot(environment="dev")
        backend.save("dev", state)
        loaded = backend.load("dev")
        assert loaded is not None
        assert loaded.environment == "dev"

    def test_save_and_load_with_assets(self, backend: SQLiteBackend):
        state = StateSnapshot(
            environment="dev",
            assets={"a": AssetState(name="a", fingerprint="fp1", kind="model")},
        )
        backend.save("dev", state)
        loaded = backend.load("dev")
        assert loaded is not None
        assert "a" in loaded.assets
        assert loaded.assets["a"].fingerprint == "fp1"
        assert loaded.assets["a"].kind == "model"

    def test_save_and_load_with_dependencies(self, backend: SQLiteBackend):
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
        loaded = backend.load("dev")
        assert loaded is not None
        assert len(loaded.dependencies) == 1
        assert loaded.dependencies[0].source == "staging.users"
        assert loaded.dependencies[0].target == "raw.users"

    def test_save_and_load_with_source_files(self, backend: SQLiteBackend):
        state = StateSnapshot(
            environment="dev",
            assets={
                "a": AssetState(
                    name="a",
                    fingerprint="fp1",
                    source_files=[
                        SourceFileRef(path="models/a.sql", content_hash="abc123"),
                    ],
                ),
            },
        )
        backend.save("dev", state)
        loaded = backend.load("dev")
        assert loaded is not None
        sf = loaded.assets["a"].source_files
        assert len(sf) == 1
        assert sf[0].path == "models/a.sql"
        assert sf[0].content_hash == "abc123"

    def test_save_and_load_with_data(self, backend: SQLiteBackend):
        state = StateSnapshot(
            environment="dev",
            assets={
                "a": AssetState(
                    name="a",
                    fingerprint="fp1",
                    data={"sql": "SELECT 1", "tags": ["core"]},
                ),
            },
        )
        backend.save("dev", state)
        loaded = backend.load("dev")
        assert loaded is not None
        assert loaded.assets["a"].data == {"sql": "SELECT 1", "tags": ["core"]}

    def test_save_and_load_with_metadata(self, backend: SQLiteBackend):
        state = StateSnapshot(
            environment="dev",
            metadata={"team": "data-eng", "version": 42},
        )
        backend.save("dev", state)
        loaded = backend.load("dev")
        assert loaded is not None
        assert loaded.metadata == {"team": "data-eng", "version": 42}

    def test_list_environments(self, backend: SQLiteBackend):
        backend.save("dev", StateSnapshot(environment="dev"))
        backend.save("prod", StateSnapshot(environment="prod"))
        envs = backend.list_environments()
        assert set(envs) == {"dev", "prod"}

    def test_delete_environment(self, backend: SQLiteBackend):
        backend.save("dev", StateSnapshot(environment="dev"))
        backend.delete_environment("dev")
        assert backend.load("dev") is None
        assert backend.list_environments() == []

    def test_delete_environment_removes_assets_and_deps(self, backend: SQLiteBackend):
        state = StateSnapshot(
            environment="dev",
            assets={"a": AssetState(name="a", fingerprint="fp1")},
            dependencies=[
                DependencyState(source="a", target="b", fingerprint="depfp1"),
            ],
        )
        backend.save("dev", state)
        backend.delete_environment("dev")
        assert backend.load("dev") is None

    def test_lock(self, backend: SQLiteBackend):
        with backend.lock("dev"):
            pass  # should succeed

    def test_double_lock_raises(self, backend: SQLiteBackend):
        with backend.lock("dev"):
            with pytest.raises(RuntimeError, match="already locked"):
                with backend.lock("dev"):
                    pass

    def test_lock_released_after_exception(self, backend: SQLiteBackend):
        with pytest.raises(ValueError):
            with backend.lock("dev"):
                raise ValueError("test")
        # Lock should be released
        with backend.lock("dev"):
            pass


class TestSQLiteBackendUpdate:
    """Tests for updating (overwriting) state."""

    def test_update_asset(self, backend: SQLiteBackend):
        state = StateSnapshot(
            environment="dev",
            assets={"a": AssetState(name="a", fingerprint="fp1")},
        )
        backend.save("dev", state)

        # Update
        state.assets["a"] = AssetState(name="a", fingerprint="fp2", kind="updated")
        backend.save("dev", state)

        loaded = backend.load("dev")
        assert loaded is not None
        assert loaded.assets["a"].fingerprint == "fp2"
        assert loaded.assets["a"].kind == "updated"

    def test_add_asset(self, backend: SQLiteBackend):
        state = StateSnapshot(
            environment="dev",
            assets={"a": AssetState(name="a", fingerprint="fp1")},
        )
        backend.save("dev", state)

        state.assets["b"] = AssetState(name="b", fingerprint="fp_b")
        backend.save("dev", state)

        loaded = backend.load("dev")
        assert loaded is not None
        assert len(loaded.assets) == 2

    def test_remove_asset(self, backend: SQLiteBackend):
        state = StateSnapshot(
            environment="dev",
            assets={
                "a": AssetState(name="a", fingerprint="fp1"),
                "b": AssetState(name="b", fingerprint="fp2"),
            },
        )
        backend.save("dev", state)

        del state.assets["b"]
        backend.save("dev", state)

        loaded = backend.load("dev")
        assert loaded is not None
        assert len(loaded.assets) == 1
        assert "a" in loaded.assets

    def test_tombstone_deleted_asset(self, backend: SQLiteBackend):
        state = StateSnapshot(
            environment="dev",
            assets={
                "a": AssetState(name="a", fingerprint="fp1", deleted=True),
            },
        )
        backend.save("dev", state)
        loaded = backend.load("dev")
        assert loaded is not None
        assert loaded.assets["a"].deleted is True


class TestSQLiteBackendIsolation:
    """Tests for environment isolation."""

    def test_environments_isolated(self, backend: SQLiteBackend):
        backend.save(
            "dev",
            StateSnapshot(
                environment="dev",
                assets={"a": AssetState(name="a", fingerprint="dev_fp")},
            ),
        )
        backend.save(
            "prod",
            StateSnapshot(
                environment="prod",
                assets={"b": AssetState(name="b", fingerprint="prod_fp")},
            ),
        )

        dev = backend.load("dev")
        prod = backend.load("prod")
        assert dev is not None and prod is not None
        assert "a" in dev.assets and "b" not in dev.assets
        assert "b" in prod.assets and "a" not in prod.assets

    def test_delete_one_environment_keeps_others(self, backend: SQLiteBackend):
        backend.save("dev", StateSnapshot(environment="dev"))
        backend.save("prod", StateSnapshot(environment="prod"))
        backend.delete_environment("dev")
        assert backend.load("dev") is None
        assert backend.load("prod") is not None


class TestSQLiteBackendHistory:
    """Tests for automatic version history via triggers."""

    def test_insert_creates_history(self, backend: SQLiteBackend):
        state = StateSnapshot(
            environment="dev",
            assets={"a": AssetState(name="a", fingerprint="fp1")},
        )
        backend.save("dev", state)

        history = backend.asset_history("dev", "a")
        assert len(history) == 1
        assert history[0]["action"] == "create"
        assert history[0]["fingerprint"] == "fp1"

    def test_update_appends_history(self, backend: SQLiteBackend):
        state = StateSnapshot(
            environment="dev",
            assets={"a": AssetState(name="a", fingerprint="fp1")},
        )
        backend.save("dev", state)

        state.assets["a"] = AssetState(name="a", fingerprint="fp2")
        backend.save("dev", state)

        history = backend.asset_history("dev", "a")
        assert len(history) == 2
        # Most recent first
        assert history[0]["action"] == "update"
        assert history[0]["fingerprint"] == "fp2"
        assert history[1]["action"] == "create"
        assert history[1]["fingerprint"] == "fp1"

    def test_delete_appends_history(self, backend: SQLiteBackend):
        state = StateSnapshot(
            environment="dev",
            assets={"a": AssetState(name="a", fingerprint="fp1")},
        )
        backend.save("dev", state)

        del state.assets["a"]
        backend.save("dev", state)

        history = backend.asset_history("dev", "a")
        assert len(history) == 2
        assert history[0]["action"] == "delete"
        assert history[1]["action"] == "create"

    def test_multiple_updates_full_history(self, backend: SQLiteBackend):
        state = StateSnapshot(
            environment="dev",
            assets={"a": AssetState(name="a", fingerprint="v1", version=1)},
        )
        backend.save("dev", state)

        for i in range(2, 6):
            state.assets["a"] = AssetState(name="a", fingerprint=f"v{i}", version=i)
            backend.save("dev", state)

        history = backend.asset_history("dev", "a")
        assert len(history) == 5
        assert history[0]["version"] == 5
        assert history[4]["version"] == 1

    def test_environment_changelog(self, backend: SQLiteBackend):
        state = StateSnapshot(
            environment="dev",
            assets={
                "a": AssetState(name="a", fingerprint="fp1"),
                "b": AssetState(name="b", fingerprint="fp2"),
            },
        )
        backend.save("dev", state)

        changelog = backend.environment_changelog("dev")
        assert len(changelog) == 2
        names = {e["name"] for e in changelog}
        assert names == {"a", "b"}

    def test_asset_version_lookup(self, backend: SQLiteBackend):
        state = StateSnapshot(
            environment="dev",
            assets={"a": AssetState(name="a", fingerprint="v1", version=1)},
        )
        backend.save("dev", state)

        state.assets["a"] = AssetState(
            name="a", fingerprint="v2", version=2, data={"sql": "SELECT 2"}
        )
        backend.save("dev", state)

        v1 = backend.asset_version("dev", "a", 1)
        assert v1 is not None
        assert v1["fingerprint"] == "v1"

        v2 = backend.asset_version("dev", "a", 2)
        assert v2 is not None
        assert v2["fingerprint"] == "v2"

        v3 = backend.asset_version("dev", "a", 3)
        assert v3 is None

    def test_history_limit(self, backend: SQLiteBackend):
        state = StateSnapshot(
            environment="dev",
            assets={"a": AssetState(name="a", fingerprint="v1", version=1)},
        )
        backend.save("dev", state)

        for i in range(2, 12):
            state.assets["a"] = AssetState(name="a", fingerprint=f"v{i}", version=i)
            backend.save("dev", state)

        history = backend.asset_history("dev", "a", limit=3)
        assert len(history) == 3
        assert history[0]["version"] == 11

    def test_history_isolated_per_environment(self, backend: SQLiteBackend):
        backend.save(
            "dev",
            StateSnapshot(
                environment="dev",
                assets={"a": AssetState(name="a", fingerprint="dev_fp")},
            ),
        )
        backend.save(
            "prod",
            StateSnapshot(
                environment="prod",
                assets={"a": AssetState(name="a", fingerprint="prod_fp")},
            ),
        )

        dev_history = backend.asset_history("dev", "a")
        prod_history = backend.asset_history("prod", "a")
        assert len(dev_history) == 1
        assert len(prod_history) == 1
        assert dev_history[0]["fingerprint"] == "dev_fp"
        assert prod_history[0]["fingerprint"] == "prod_fp"

    def test_history_data_preserved(self, backend: SQLiteBackend):
        state = StateSnapshot(
            environment="dev",
            assets={
                "a": AssetState(
                    name="a",
                    fingerprint="fp1",
                    data={"sql": "SELECT 1", "columns": ["id", "name"]},
                    applied_by="alice",
                ),
            },
        )
        backend.save("dev", state)

        history = backend.asset_history("dev", "a")
        assert len(history) == 1
        assert history[0]["applied_by"] == "alice"
        # data is stored as JSON string in history
        import json

        data = json.loads(history[0]["data"])
        assert data["sql"] == "SELECT 1"
