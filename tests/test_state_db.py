"""Tests for low-level SQLite schema bootstrap."""

from __future__ import annotations

from pathlib import Path

from assets.state.db import connect_state


def test_connect_state_in_memory_creates_core_tables() -> None:
    conn = connect_state(":memory:")
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
        names = {row["name"] for row in rows}

        assert "environments" in names
        assert "assets" in names
        assert "dependencies" in names
        assert "assets_history" in names
        assert "dependencies_history" in names
        assert "index_entries" in names
        assert "index_deps" in names
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
