"""Tests for SQLiteCompiledCache — mirrors test_compiled_cache.py."""

import json
import time
from pathlib import Path

import pytest

from assets import SQLiteCompiledCache


@pytest.fixture
def tmp_project(tmp_path: Path) -> Path:
    """Create a temp project with a source file."""
    src = tmp_path / "models" / "users.json"
    src.parent.mkdir(parents=True)
    src.write_text(json.dumps({"name": "users", "kind": "model"}))
    return tmp_path


@pytest.fixture
def cache(tmp_path: Path) -> SQLiteCompiledCache:
    c = SQLiteCompiledCache(db_path=tmp_path / ".cache" / "compiled.db")
    yield c
    c.close()


class TestSQLiteCompiledCache:
    def test_miss_on_empty_cache(self, cache: SQLiteCompiledCache, tmp_project: Path):
        src = tmp_project / "models" / "users.json"
        assert cache.get(src, tmp_project) is None

    def test_put_and_get(self, cache: SQLiteCompiledCache, tmp_project: Path):
        src = tmp_project / "models" / "users.json"
        data = {"name": "users", "kind": "model"}
        cache.put(src, tmp_project, data)
        result = cache.get(src, tmp_project)
        assert result == data

    def test_mtime_fast_path(self, cache: SQLiteCompiledCache, tmp_project: Path):
        src = tmp_project / "models" / "users.json"
        data = {"name": "users"}
        cache.put(src, tmp_project, data)

        # Same file, same mtime → cache hit
        result = cache.get(src, tmp_project)
        assert result == data

    def test_content_change_invalidates(
        self, cache: SQLiteCompiledCache, tmp_project: Path
    ):
        src = tmp_project / "models" / "users.json"
        data = {"name": "users"}
        cache.put(src, tmp_project, data)

        # Modify file content
        time.sleep(0.01)
        src.write_text(json.dumps({"name": "users_v2"}))

        result = cache.get(src, tmp_project)
        assert result is None

    def test_mtime_change_same_content(
        self, cache: SQLiteCompiledCache, tmp_project: Path
    ):
        src = tmp_project / "models" / "users.json"
        original_content = src.read_text()
        data = {"name": "users", "kind": "model"}
        cache.put(src, tmp_project, data)

        # Rewrite same content (simulates git checkout)
        time.sleep(0.01)
        src.write_text(original_content)

        result = cache.get(src, tmp_project)
        assert result == data

    def test_source_deleted_returns_none(
        self, cache: SQLiteCompiledCache, tmp_project: Path
    ):
        src = tmp_project / "models" / "users.json"
        cache.put(src, tmp_project, {"name": "users"})

        src.unlink()
        result = cache.get(src, tmp_project)
        assert result is None

    def test_clean_removes_orphans(
        self, cache: SQLiteCompiledCache, tmp_project: Path
    ):
        src = tmp_project / "models" / "users.json"
        cache.put(src, tmp_project, {"name": "users"})

        # Delete source file
        src.unlink()

        removed = cache.clean(tmp_project)
        assert removed == 1

    def test_clean_keeps_valid(
        self, cache: SQLiteCompiledCache, tmp_project: Path
    ):
        src = tmp_project / "models" / "users.json"
        cache.put(src, tmp_project, {"name": "users"})

        removed = cache.clean(tmp_project)
        assert removed == 0

    def test_overwrite_existing_entry(
        self, cache: SQLiteCompiledCache, tmp_project: Path
    ):
        src = tmp_project / "models" / "users.json"
        cache.put(src, tmp_project, {"name": "users_v1"})
        cache.put(src, tmp_project, {"name": "users_v2"})

        result = cache.get(src, tmp_project)
        assert result == {"name": "users_v2"}

    def test_multiple_files(
        self, cache: SQLiteCompiledCache, tmp_project: Path
    ):
        src1 = tmp_project / "models" / "users.json"
        src2 = tmp_project / "models" / "payments.json"
        src2.write_text(json.dumps({"name": "payments"}))

        cache.put(src1, tmp_project, {"name": "users"})
        cache.put(src2, tmp_project, {"name": "payments"})

        assert cache.get(src1, tmp_project) == {"name": "users"}
        assert cache.get(src2, tmp_project) == {"name": "payments"}

    def test_clean_selective(
        self, cache: SQLiteCompiledCache, tmp_project: Path
    ):
        src1 = tmp_project / "models" / "users.json"
        src2 = tmp_project / "models" / "payments.json"
        src2.write_text(json.dumps({"name": "payments"}))

        cache.put(src1, tmp_project, {"name": "users"})
        cache.put(src2, tmp_project, {"name": "payments"})

        # Delete only one source
        src2.unlink()

        removed = cache.clean(tmp_project)
        assert removed == 1

        # users should still be cached
        assert cache.get(src1, tmp_project) == {"name": "users"}
