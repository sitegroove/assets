"""Tests for low-level SQLite schema bootstrap."""

from __future__ import annotations

from pathlib import Path

from assets.state.db import connect_index, connect_state


def test_connect_state_in_memory_creates_core_tables() -> None:
    conn = connect_state(":memory:")
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
        names = {row["name"] for row in rows}

        assert "state_metadata" in names
        assert "assets" in names
        assert "dependencies" in names
        assert "assets_history" in names
        assert "dependencies_history" in names
        # index tables are now in a separate DB
        assert "index_entries" not in names
        assert "index_deps" not in names
    finally:
        conn.close()


def test_connect_index_in_memory_creates_index_tables() -> None:
    conn = connect_index(":memory:")
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
        names = {row["name"] for row in rows}

        assert "index_entries" in names
        assert "index_deps" in names
        # state tables should NOT be in the index DB
        assert "assets" not in names
        assert "dependencies" not in names
    finally:
        conn.close()


def test_connect_state_creates_parent_directory(tmp_path: Path) -> None:
    db_path = tmp_path / "state" / "nested" / "state.db"

    conn = connect_state(db_path)
    try:
        assert db_path.exists()
    finally:
        conn.close()


def test_connect_state_enables_foreign_keys() -> None:
    conn = connect_state(":memory:")
    try:
        pragma = conn.execute("PRAGMA foreign_keys").fetchone()
        assert pragma is not None
        assert pragma[0] == 1
    finally:
        conn.close()


def test_state_metadata_is_singleton() -> None:
    """Regression: state_metadata must enforce exactly one row.

    The table has a CHECK(id = 1) constraint so only rowid 1 is
    valid.  Attempting a second insert with a different id must fail.
    """
    import sqlite3

    conn = connect_state(":memory:")
    try:
        conn.execute(
            "INSERT INTO state_metadata (id, version, created_at, updated_at) "
            "VALUES (1, 1, '2025-01-01T00:00:00Z', '2025-01-01T00:00:00Z')"
        )
        # Second insert with id=1 should conflict on PK
        try:
            conn.execute(
                "INSERT INTO state_metadata (id, version, created_at, updated_at) "
                "VALUES (1, 2, '2025-01-01T00:00:00Z', '2025-01-01T00:00:00Z')"
            )
            assert False, "Should have raised IntegrityError"
        except sqlite3.IntegrityError:
            pass  # expected: PK conflict

        # Insert with id=2 should be rejected by CHECK constraint
        try:
            conn.execute(
                "INSERT INTO state_metadata (id, version, created_at, updated_at) "
                "VALUES (2, 1, '2025-01-01T00:00:00Z', '2025-01-01T00:00:00Z')"
            )
            assert False, "Should have raised IntegrityError"
        except sqlite3.IntegrityError:
            pass  # expected: CHECK(id = 1) violation
    finally:
        conn.close()
