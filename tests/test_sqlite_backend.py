"""Tests for SQLiteBackend — state persistence with version history."""

from datetime import datetime, timedelta, timezone
from collections.abc import Generator
from pathlib import Path

import pytest

from assets import SQLiteBackend
from assets.state.sqlite import _parse_dt
from assets.state.models import AssetState, DependencyState, StateSnapshot


@pytest.fixture
def backend(tmp_path: Path) -> Generator[SQLiteBackend, None, None]:
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
            assets={"a": AssetState(id="a", fingerprint="fp1", type="model")},
        )
        backend.save("dev", state)
        loaded = backend.load("dev")
        assert loaded is not None
        assert "a" in loaded.assets
        assert loaded.assets["a"].fingerprint == "fp1"
        assert loaded.assets["a"].type == "model"

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

    def test_save_and_load_with_data(self, backend: SQLiteBackend):
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
            assets={"a": AssetState(id="a", fingerprint="fp1")},
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
            assets={"a": AssetState(id="a", fingerprint="fp1")},
        )
        backend.save("dev", state)

        # Update
        state.assets["a"] = AssetState(id="a", fingerprint="fp2", type="updated")
        backend.save("dev", state)

        loaded = backend.load("dev")
        assert loaded is not None
        assert loaded.assets["a"].fingerprint == "fp2"
        assert loaded.assets["a"].type == "updated"

    def test_add_asset(self, backend: SQLiteBackend):
        state = StateSnapshot(
            environment="dev",
            assets={"a": AssetState(id="a", fingerprint="fp1")},
        )
        backend.save("dev", state)

        state.assets["b"] = AssetState(id="b", fingerprint="fp_b")
        backend.save("dev", state)

        loaded = backend.load("dev")
        assert loaded is not None
        assert len(loaded.assets) == 2

    def test_remove_asset(self, backend: SQLiteBackend):
        state = StateSnapshot(
            environment="dev",
            assets={
                "a": AssetState(id="a", fingerprint="fp1"),
                "b": AssetState(id="b", fingerprint="fp2"),
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
                "a": AssetState(id="a", fingerprint="fp1", deleted=True),
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
                assets={"a": AssetState(id="a", fingerprint="dev_fp")},
            ),
        )
        backend.save(
            "prod",
            StateSnapshot(
                environment="prod",
                assets={"b": AssetState(id="b", fingerprint="prod_fp")},
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
            assets={"a": AssetState(id="a", fingerprint="fp1")},
        )
        backend.save("dev", state)

        history = backend.asset_history("dev", "a")
        assert len(history) == 1
        assert history[0]["action"] == "create"
        assert history[0]["fingerprint"] == "fp1"

    def test_update_appends_history(self, backend: SQLiteBackend):
        state = StateSnapshot(
            environment="dev",
            assets={"a": AssetState(id="a", fingerprint="fp1")},
        )
        backend.save("dev", state)

        state.assets["a"] = AssetState(id="a", fingerprint="fp2")
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
            assets={"a": AssetState(id="a", fingerprint="fp1")},
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
            assets={"a": AssetState(id="a", fingerprint="v1", version=1)},
        )
        backend.save("dev", state)

        for i in range(2, 6):
            state.assets["a"] = AssetState(id="a", fingerprint=f"v{i}", version=i)
            backend.save("dev", state)

        history = backend.asset_history("dev", "a")
        assert len(history) == 5
        assert history[0]["version"] == 5
        assert history[4]["version"] == 1

    def test_environment_changelog(self, backend: SQLiteBackend):
        state = StateSnapshot(
            environment="dev",
            assets={
                "a": AssetState(id="a", fingerprint="fp1"),
                "b": AssetState(id="b", fingerprint="fp2"),
            },
        )
        backend.save("dev", state)

        changelog = backend.environment_changelog("dev")
        assert len(changelog) == 2
        names = {e["asset_id"] for e in changelog}
        assert names == {"a", "b"}

    def test_asset_version_lookup(self, backend: SQLiteBackend):
        state = StateSnapshot(
            environment="dev",
            assets={"a": AssetState(id="a", fingerprint="v1", version=1)},
        )
        backend.save("dev", state)

        state.assets["a"] = AssetState(
            id="a", fingerprint="v2", version=2, data={"sql": "SELECT 2"}
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
            assets={"a": AssetState(id="a", fingerprint="v1", version=1)},
        )
        backend.save("dev", state)

        for i in range(2, 12):
            state.assets["a"] = AssetState(id="a", fingerprint=f"v{i}", version=i)
            backend.save("dev", state)

        history = backend.asset_history("dev", "a", limit=3)
        assert len(history) == 3
        assert history[0]["version"] == 11

    def test_history_isolated_per_environment(self, backend: SQLiteBackend):
        backend.save(
            "dev",
            StateSnapshot(
                environment="dev",
                assets={"a": AssetState(id="a", fingerprint="dev_fp")},
            ),
        )
        backend.save(
            "prod",
            StateSnapshot(
                environment="prod",
                assets={"a": AssetState(id="a", fingerprint="prod_fp")},
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
                    id="a",
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

    def test_environment_changelog_since_filters(self, backend: SQLiteBackend) -> None:
        state = StateSnapshot(
            environment="dev",
            assets={"a": AssetState(id="a", fingerprint="fp1")},
        )
        backend.save("dev", state)

        all_rows = backend.environment_changelog(
            "dev",
            since="1970-01-01T00:00:00.000Z",
        )
        assert len(all_rows) >= 1

        future_since = (datetime.now(timezone.utc) + timedelta(days=1)).strftime(
            "%Y-%m-%dT%H:%M:%S.%fZ"
        )
        assert backend.environment_changelog("dev", since=future_since) == []


class TestDatetimeParsing:
    def test_parse_dt_fallback_isoformat_with_offset(self) -> None:
        parsed = _parse_dt("2026-01-01T10:20:30+00:00")
        assert parsed.tzinfo is not None
        assert parsed.year == 2026


class TestSQLiteBackendPruneHistory:
    """Test prune_history on SQLiteBackend directly."""

    def test_prune_removes_old_entries(self, tmp_path: Path) -> None:
        backend = SQLiteBackend(db_path=tmp_path / "state.db")
        state = StateSnapshot(
            environment="dev",
            assets={"a": AssetState(id="a", fingerprint="v1", version=1)},
        )
        backend.save("dev", state)

        # Backdate all history
        backend.conn.execute(
            "UPDATE assets_history SET recorded_at = '2020-01-01T00:00:00.000Z'"
        )
        backend.conn.commit()

        count = backend.prune_history(keep_days=0)
        assert count >= 1

        # Verify history is gone
        history = backend.asset_history("dev", "a")
        assert len(history) == 0

    def test_prune_keeps_recent_entries(self, tmp_path: Path) -> None:
        backend = SQLiteBackend(db_path=tmp_path / "state.db")
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


class TestDependencyHistoryTriggers:
    """Verify triggers populate dependencies_history table."""

    def test_insert_trigger_creates_history(self, tmp_path: Path) -> None:
        backend = SQLiteBackend(db_path=tmp_path / "state.db")
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

        rows = backend.conn.execute(
            "SELECT * FROM dependencies_history WHERE environment = 'dev'"
        ).fetchall()
        assert len(rows) == 1
        assert rows[0]["action"] == "create"
        assert rows[0]["source"] == "staging.users"
        assert rows[0]["target"] == "raw.users"

    def test_delete_trigger_creates_history(self, tmp_path: Path) -> None:
        backend = SQLiteBackend(db_path=tmp_path / "state.db")

        # Insert dependency
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

        rows = backend.conn.execute(
            "SELECT * FROM dependencies_history WHERE environment = 'dev' ORDER BY id"
        ).fetchall()
        # First: create, second: delete
        assert len(rows) == 2
        assert rows[0]["action"] == "create"
        assert rows[1]["action"] == "delete"
        assert rows[1]["source"] == "staging.users"

    def test_update_trigger_creates_history(self, tmp_path: Path) -> None:
        """Updating a dependency's fingerprint fires the update trigger.

        Note: SQLiteBackend.save() deletes all deps then re-inserts.
        So an "update" in practice is delete+insert, yielding delete+create
        history entries. This test verifies that dep changes are tracked.
        """
        backend = SQLiteBackend(db_path=tmp_path / "state.db")

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

        rows = backend.conn.execute(
            "SELECT * FROM dependencies_history WHERE environment = 'dev' ORDER BY id"
        ).fetchall()
        # create from first save, delete+create from second save
        assert len(rows) >= 2
        actions = [r["action"] for r in rows]
        assert "create" in actions
