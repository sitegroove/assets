"""Tests for FileIndex (SQLite-backed freshness tracker)."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from assets.index.file import FileIndex, MtimeCache
from assets.state.db import connect_state


def _bump_mtime(path: Path, delta_ns: int = 1_000_000_000) -> None:
    """Advance a file's mtime by *delta_ns* nanoseconds (default 1 s)."""
    stat = path.stat()
    new_ns = stat.st_mtime_ns + delta_ns
    os.utime(path, ns=(stat.st_atime_ns, new_ns))


@pytest.fixture
def tmp_project(tmp_path: Path) -> Path:
    """Create a temp project with a source YAML file."""
    src = tmp_path / "models" / "users.yaml"
    src.parent.mkdir(parents=True)
    src.write_text(json.dumps({"id": "raw.users", "type": "source"}))
    return tmp_path


@pytest.fixture
def index(tmp_path: Path) -> FileIndex:
    """FileIndex backed by a temp state database."""
    local_path = tmp_path / ".state"
    conn = connect_state(local_path / "state.db")
    return FileIndex(conn, local_path)


# ── MtimeCache unit tests ───────────────────────────────────


class TestMtimeCache:
    def test_in_memory_only(self) -> None:
        cache = MtimeCache()
        cache.set("a.yaml", 100)
        assert cache.get("a.yaml") == 100
        cache.flush()  # no-op, shouldn't raise

    def test_persistence(self, tmp_path: Path) -> None:
        path = tmp_path / "cache.json"
        cache = MtimeCache(path)
        cache.set("a.yaml", 100)
        cache.set("b.yaml", 200)
        cache.flush()

        # Reload from disk
        cache2 = MtimeCache(path)
        assert cache2.get("a.yaml") == 100
        assert cache2.get("b.yaml") == 200

    def test_discard(self) -> None:
        cache = MtimeCache()
        cache.set("a.yaml", 100)
        cache.discard("a.yaml")
        assert cache.get("a.yaml") is None

    def test_discard_missing_no_error(self) -> None:
        cache = MtimeCache()
        cache.discard("nonexistent")  # should not raise

    def test_corrupted_file_starts_fresh(self, tmp_path: Path) -> None:
        path = tmp_path / "cache.json"
        path.write_text("not valid json")
        cache = MtimeCache(path)
        assert cache.get("anything") is None

    def test_missing_file_starts_fresh(self, tmp_path: Path) -> None:
        path = tmp_path / "nonexistent" / "cache.json"
        cache = MtimeCache(path)
        assert cache.get("anything") is None


# ── basic put_file / remove ──────────────────────────────────


class TestFileIndexBasics:
    def test_put_file_and_remove(self, index: FileIndex, tmp_project: Path) -> None:
        src = tmp_project / "models" / "users.yaml"
        index.put_file(
            src,
            tmp_project,
            asset_id="raw.users",
            fingerprint="abc123",
        )
        assert index.remove("models/users.yaml") is True

    def test_remove_missing_returns_false(self, index: FileIndex) -> None:
        assert index.remove("nonexistent") is False

    def test_put_file_overwrites(self, index: FileIndex, tmp_project: Path) -> None:
        src = tmp_project / "models" / "users.yaml"
        index.put_file(
            src,
            tmp_project,
            asset_id="raw.users",
            fingerprint="fp1",
        )
        index.put_file(
            src,
            tmp_project,
            asset_id="raw.users",
            fingerprint="fp2",
        )
        # Verify overwrite via diff — should be fresh
        discovered = [(src, src.stat().st_mtime_ns)]
        status = index.diff(discovered, tmp_project)
        assert len(status.fresh) == 1


# ── diff() classification ────────────────────────────────────


class TestFileIndexDiff:
    def test_empty_index_all_new(self, index: FileIndex, tmp_project: Path) -> None:
        src = tmp_project / "models" / "users.yaml"
        discovered = [(src, src.stat().st_mtime_ns)]
        status = index.diff(discovered, tmp_project)

        assert len(status.new) == 1
        assert len(status.fresh) == 0
        assert len(status.changed) == 0
        assert len(status.deleted) == 0

    def test_unchanged_file_is_fresh(self, index: FileIndex, tmp_project: Path) -> None:
        src = tmp_project / "models" / "users.yaml"
        index.put_file(
            src,
            tmp_project,
            asset_id="raw.users",
            fingerprint="abc",
        )
        discovered = [(src, src.stat().st_mtime_ns)]
        status = index.diff(discovered, tmp_project)

        assert len(status.fresh) == 1
        assert len(status.new) == 0
        assert len(status.changed) == 0

    def test_fresh_carries_asset_id(self, index: FileIndex, tmp_project: Path) -> None:
        """Fresh entries include (path, asset_id) for state lookup."""
        src = tmp_project / "models" / "users.yaml"
        index.put_file(
            src,
            tmp_project,
            asset_id="raw.users",
            fingerprint="abc",
        )
        discovered = [(src, src.stat().st_mtime_ns)]
        status = index.diff(discovered, tmp_project)

        assert len(status.fresh) == 1
        path, asset_id = status.fresh[0]
        assert path == src
        assert asset_id == "raw.users"
        assert status.fresh_ids == ["raw.users"]

    def test_changed_file_content(self, index: FileIndex, tmp_project: Path) -> None:
        src = tmp_project / "models" / "users.yaml"
        index.put_file(
            src,
            tmp_project,
            asset_id="raw.users",
            fingerprint="abc",
        )
        # Change file content + bump mtime
        src.write_text(json.dumps({"id": "raw.users", "extra": True}))
        _bump_mtime(src)

        discovered = [(src, src.stat().st_mtime_ns)]
        status = index.diff(discovered, tmp_project)

        assert len(status.changed) == 1
        assert len(status.fresh) == 0

    def test_deleted_file(self, index: FileIndex, tmp_project: Path) -> None:
        src = tmp_project / "models" / "users.yaml"
        index.put_file(
            src,
            tmp_project,
            asset_id="raw.users",
            fingerprint="abc",
        )
        # Discover nothing
        status = index.diff([], tmp_project)

        assert len(status.deleted) == 1
        loc, asset_id = status.deleted[0]
        assert asset_id == "raw.users"
        assert "users.yaml" in loc

    def test_stale_returns_changed_plus_new(
        self, index: FileIndex, tmp_project: Path
    ) -> None:
        src = tmp_project / "models" / "users.yaml"
        new_file = tmp_project / "models" / "orders.yaml"
        new_file.write_text(json.dumps({"id": "raw.orders"}))

        index.put_file(
            src,
            tmp_project,
            asset_id="raw.users",
            fingerprint="abc",
        )
        # Change users.yaml
        src.write_text(json.dumps({"id": "raw.users", "v": 2}))
        _bump_mtime(src)

        discovered = [
            (src, src.stat().st_mtime_ns),
            (new_file, new_file.stat().st_mtime_ns),
        ]
        status = index.diff(discovered, tmp_project)
        assert len(status.stale) == 2

    def test_mtime_differs_but_hash_matches_is_fresh(
        self, index: FileIndex, tmp_project: Path
    ) -> None:
        """Simulates git checkout: mtime changes but content same."""
        src = tmp_project / "models" / "users.yaml"
        index.put_file(
            src,
            tmp_project,
            asset_id="raw.users",
            fingerprint="abc",
        )
        # Bump mtime without changing content (like git checkout)
        _bump_mtime(src)

        discovered = [(src, src.stat().st_mtime_ns)]
        status = index.diff(discovered, tmp_project)

        assert len(status.fresh) == 1
        assert len(status.changed) == 0

    def test_mtime_change_does_not_dirty_sqlite(
        self, index: FileIndex, tmp_project: Path
    ) -> None:
        """Save/revert: mtime changes, content same -> no SQLite write.

        This is the key invariant: mtime-only changes must NOT modify
        state.db, preventing unnecessary remote pushes.
        """
        src = tmp_project / "models" / "users.yaml"
        index.put_file(
            src,
            tmp_project,
            asset_id="raw.users",
            fingerprint="abc",
        )

        # Record SQLite state (total_changes counter)
        changes_before = index.conn.total_changes

        # Bump mtime without changing content
        _bump_mtime(src)

        discovered = [(src, src.stat().st_mtime_ns)]
        status = index.diff(discovered, tmp_project)

        assert len(status.fresh) == 1
        # The critical assertion: no SQL writes happened
        assert index.conn.total_changes == changes_before


# ── dependency tracking ──────────────────────────────────────


class TestFileIndexDeps:
    def test_dep_change_triggers_stale(
        self, index: FileIndex, tmp_project: Path
    ) -> None:
        src = tmp_project / "models" / "users.yaml"
        sql = tmp_project / "models" / "users.sql"
        sql.write_text("SELECT * FROM raw_users")

        index.put_file(
            src,
            tmp_project,
            asset_id="staging.users",
            fingerprint="abc",
            deps=[(sql, "sql")],
        )

        # Change the SQL file
        sql.write_text("SELECT id, email FROM raw_users")
        _bump_mtime(sql)

        discovered = [(src, src.stat().st_mtime_ns)]
        status = index.diff(discovered, tmp_project)

        assert len(status.changed) == 1
        assert len(status.fresh) == 0

    def test_dep_unchanged_stays_fresh(
        self, index: FileIndex, tmp_project: Path
    ) -> None:
        src = tmp_project / "models" / "users.yaml"
        sql = tmp_project / "models" / "users.sql"
        sql.write_text("SELECT * FROM raw_users")

        index.put_file(
            src,
            tmp_project,
            asset_id="staging.users",
            fingerprint="abc",
            deps=[(sql, "sql")],
        )

        discovered = [(src, src.stat().st_mtime_ns)]
        status = index.diff(discovered, tmp_project)

        assert len(status.fresh) == 1

    def test_dep_deleted_triggers_stale(
        self, index: FileIndex, tmp_project: Path
    ) -> None:
        src = tmp_project / "models" / "users.yaml"
        sql = tmp_project / "models" / "users.sql"
        sql.write_text("SELECT 1")

        index.put_file(
            src,
            tmp_project,
            asset_id="staging.users",
            fingerprint="abc",
            deps=[(sql, "sql")],
        )

        # Delete the dep file
        sql.unlink()

        discovered = [(src, src.stat().st_mtime_ns)]
        status = index.diff(discovered, tmp_project)

        assert len(status.changed) == 1

    def test_stale_entries_reverse_lookup(
        self, index: FileIndex, tmp_project: Path
    ) -> None:
        src = tmp_project / "models" / "users.yaml"
        macro = tmp_project / "macros" / "key.sql"
        macro.parent.mkdir(parents=True)
        macro.write_text("{% macro key() %}md5(id){% endmacro %}")

        index.put_file(
            src,
            tmp_project,
            asset_id="staging.users",
            fingerprint="abc",
            deps=[(macro, "macro")],
        )

        stale = index.stale_entries({"macros/key.sql"})
        assert "models/users.yaml" in stale

    def test_stale_entries_empty_input(self, index: FileIndex) -> None:
        assert index.stale_entries(set()) == set()

    def test_multiple_deps(self, index: FileIndex, tmp_project: Path) -> None:
        src = tmp_project / "models" / "users.yaml"
        sql = tmp_project / "models" / "users.sql"
        macro = tmp_project / "macros" / "key.sql"
        sql.write_text("SELECT 1")
        macro.parent.mkdir(parents=True)
        macro.write_text("{% macro key() %}{% endmacro %}")

        index.put_file(
            src,
            tmp_project,
            asset_id="staging.users",
            fingerprint="abc",
            deps=[(sql, "sql"), (macro, "macro")],
        )

        # Only macro changes
        macro.write_text("{% macro key() %}sha256(id){% endmacro %}")
        _bump_mtime(macro)

        discovered = [(src, src.stat().st_mtime_ns)]
        status = index.diff(discovered, tmp_project)

        assert len(status.changed) == 1

    def test_dep_mtime_differs_hash_matches_stays_fresh(
        self, index: FileIndex, tmp_project: Path
    ) -> None:
        """Dep mtime changed but content unchanged (git checkout)."""
        src = tmp_project / "models" / "users.yaml"
        sql = tmp_project / "models" / "users.sql"
        sql.write_text("SELECT 1")

        index.put_file(
            src,
            tmp_project,
            asset_id="staging.users",
            fingerprint="abc",
            deps=[(sql, "sql")],
        )

        # Bump SQL mtime without changing content
        _bump_mtime(sql)

        discovered = [(src, src.stat().st_mtime_ns)]
        status = index.diff(discovered, tmp_project)

        assert len(status.fresh) == 1

    def test_dep_mtime_change_does_not_dirty_sqlite(
        self, index: FileIndex, tmp_project: Path
    ) -> None:
        """Dep mtime-only change must not write to SQLite."""
        src = tmp_project / "models" / "users.yaml"
        sql = tmp_project / "models" / "users.sql"
        sql.write_text("SELECT 1")

        index.put_file(
            src,
            tmp_project,
            asset_id="staging.users",
            fingerprint="abc",
            deps=[(sql, "sql")],
        )

        changes_before = index.conn.total_changes

        # Bump SQL mtime without changing content
        _bump_mtime(sql)

        discovered = [(src, src.stat().st_mtime_ns)]
        status = index.diff(discovered, tmp_project)

        assert len(status.fresh) == 1
        assert index.conn.total_changes == changes_before


# ── clean() ──────────────────────────────────────────────────


class TestFileIndexClean:
    def test_clean_removes_orphaned(self, index: FileIndex, tmp_project: Path) -> None:
        src = tmp_project / "models" / "users.yaml"
        index.put_file(
            src,
            tmp_project,
            asset_id="raw.users",
            fingerprint="abc",
        )
        src.unlink()
        removed = index.clean(tmp_project)
        assert removed == 1

    def test_clean_keeps_existing(self, index: FileIndex, tmp_project: Path) -> None:
        src = tmp_project / "models" / "users.yaml"
        index.put_file(
            src,
            tmp_project,
            asset_id="raw.users",
            fingerprint="abc",
        )
        removed = index.clean(tmp_project)
        assert removed == 0


# ── group isolation ──────────────────────────────────────────


class TestFileIndexGroups:
    def test_same_location_different_groups_coexist(
        self, index: FileIndex, tmp_project: Path
    ) -> None:
        """Two groups can have the same relative location without collision."""
        src = tmp_project / "models" / "users.yaml"

        index.put_file(
            src,
            tmp_project,
            group="alpha",
            asset_id="alpha.users",
            fingerprint="fp_alpha",
        )
        index.put_file(
            src,
            tmp_project,
            group="beta",
            asset_id="beta.users",
            fingerprint="fp_beta",
        )

        # Both entries exist independently
        discovered = [(src, src.stat().st_mtime_ns)]
        status_a = index.diff(discovered, tmp_project, group="alpha")
        status_b = index.diff(discovered, tmp_project, group="beta")

        assert len(status_a.fresh) == 1
        assert status_a.fresh[0][1] == "alpha.users"
        assert len(status_b.fresh) == 1
        assert status_b.fresh[0][1] == "beta.users"

    def test_remove_scoped_to_group(self, index: FileIndex, tmp_project: Path) -> None:
        """Removing an entry in one group does not affect other groups."""
        src = tmp_project / "models" / "users.yaml"

        index.put_file(
            src,
            tmp_project,
            group="alpha",
            asset_id="alpha.users",
            fingerprint="fp1",
        )
        index.put_file(
            src,
            tmp_project,
            group="beta",
            asset_id="beta.users",
            fingerprint="fp2",
        )

        assert index.remove("models/users.yaml", group="alpha") is True

        # Alpha gone, beta still there
        discovered = [(src, src.stat().st_mtime_ns)]
        status_a = index.diff(discovered, tmp_project, group="alpha")
        status_b = index.diff(discovered, tmp_project, group="beta")

        assert len(status_a.new) == 1
        assert len(status_b.fresh) == 1

    def test_diff_scoped_to_group(self, index: FileIndex, tmp_project: Path) -> None:
        """diff() only sees entries from its own group."""
        src = tmp_project / "models" / "users.yaml"

        index.put_file(
            src,
            tmp_project,
            group="alpha",
            asset_id="alpha.users",
            fingerprint="fp1",
        )

        # Diff against a different group sees no indexed entries
        discovered = [(src, src.stat().st_mtime_ns)]
        status = index.diff(discovered, tmp_project, group="beta")
        assert len(status.new) == 1
        assert len(status.fresh) == 0

    def test_clean_scoped_to_group(self, index: FileIndex, tmp_project: Path) -> None:
        """clean() only removes orphans from its own group."""
        src = tmp_project / "models" / "users.yaml"

        index.put_file(
            src,
            tmp_project,
            group="alpha",
            asset_id="alpha.users",
            fingerprint="fp1",
        )
        index.put_file(
            src,
            tmp_project,
            group="beta",
            asset_id="beta.users",
            fingerprint="fp2",
        )

        src.unlink()

        # Clean only alpha
        removed = index.clean(tmp_project, group="alpha")
        assert removed == 1

        # Beta still has the entry (shows as changed since file is gone,
        # but the entry itself was not cleaned)
        rows = index.conn.execute("SELECT grp, location FROM index_entries").fetchall()
        assert len(rows) == 1
        assert rows[0]["grp"] == "beta"

    def test_stale_entries_scoped_to_group(
        self, index: FileIndex, tmp_project: Path
    ) -> None:
        """stale_entries() only returns entries from the queried group."""
        src = tmp_project / "models" / "users.yaml"
        dep = tmp_project / "macros" / "key.sql"
        dep.parent.mkdir(parents=True)
        dep.write_text("{% macro key() %}{% endmacro %}")

        index.put_file(
            src,
            tmp_project,
            group="alpha",
            asset_id="alpha.users",
            fingerprint="fp1",
            deps=[(dep, "macro")],
        )
        index.put_file(
            src,
            tmp_project,
            group="beta",
            asset_id="beta.users",
            fingerprint="fp2",
            deps=[(dep, "macro")],
        )

        stale_a = index.stale_entries({"macros/key.sql"}, group="alpha")
        stale_b = index.stale_entries({"macros/key.sql"}, group="beta")

        assert "models/users.yaml" in stale_a
        assert "models/users.yaml" in stale_b

        # Different group returns nothing
        stale_c = index.stale_entries({"macros/key.sql"}, group="gamma")
        assert len(stale_c) == 0

    def test_dep_change_in_one_group_does_not_affect_other(
        self, index: FileIndex, tmp_project: Path
    ) -> None:
        """Dep staleness is correctly scoped per group."""
        src = tmp_project / "models" / "users.yaml"
        dep_a = tmp_project / "deps" / "schema_a.sql"
        dep_b = tmp_project / "deps" / "schema_b.sql"
        dep_a.parent.mkdir(parents=True)
        dep_a.write_text("CREATE TABLE a")
        dep_b.write_text("CREATE TABLE b")

        index.put_file(
            src,
            tmp_project,
            group="alpha",
            asset_id="alpha.users",
            fingerprint="fp1",
            deps=[(dep_a, "sql")],
        )
        index.put_file(
            src,
            tmp_project,
            group="beta",
            asset_id="beta.users",
            fingerprint="fp2",
            deps=[(dep_b, "sql")],
        )

        # Change dep_a only
        dep_a.write_text("CREATE TABLE a_v2")
        _bump_mtime(dep_a)

        discovered = [(src, src.stat().st_mtime_ns)]

        status_a = index.diff(discovered, tmp_project, group="alpha")
        status_b = index.diff(discovered, tmp_project, group="beta")

        # Alpha stale (dep_a changed), beta fresh (dep_b unchanged)
        assert len(status_a.changed) == 1
        assert len(status_b.fresh) == 1
