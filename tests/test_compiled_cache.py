"""Tests for CompiledCache."""

import json
import time
from pathlib import Path

import pytest

from assets import CompiledCache


@pytest.fixture
def tmp_project(tmp_path: Path) -> Path:
    """Create a temp project with a source file."""
    src = tmp_path / "models" / "users.json"
    src.parent.mkdir(parents=True)
    src.write_text(json.dumps({"name": "users", "kind": "model"}))
    return tmp_path


@pytest.fixture
def cache(tmp_path: Path) -> CompiledCache:
    return CompiledCache(cache_dir=str(tmp_path / ".cache"))


class TestCompiledCache:
    def test_miss_on_empty_cache(self, cache: CompiledCache, tmp_project: Path):
        src = tmp_project / "models" / "users.json"
        assert cache.get(src, tmp_project) is None

    def test_put_and_get(self, cache: CompiledCache, tmp_project: Path):
        src = tmp_project / "models" / "users.json"
        data = {"name": "users", "kind": "model"}
        cache.put(src, tmp_project, data)
        result = cache.get(src, tmp_project)
        assert result == data

    def test_mtime_fast_path(self, cache: CompiledCache, tmp_project: Path):
        src = tmp_project / "models" / "users.json"
        data = {"name": "users"}
        cache.put(src, tmp_project, data)

        # Same file, same mtime → cache hit
        result = cache.get(src, tmp_project)
        assert result == data

    def test_content_change_invalidates(self, cache: CompiledCache, tmp_project: Path):
        src = tmp_project / "models" / "users.json"
        data = {"name": "users"}
        cache.put(src, tmp_project, data)

        # Modify file content
        time.sleep(0.01)  # ensure different mtime
        src.write_text(json.dumps({"name": "users_v2"}))

        result = cache.get(src, tmp_project)
        assert result is None

    def test_mtime_change_same_content(self, cache: CompiledCache, tmp_project: Path):
        src = tmp_project / "models" / "users.json"
        original_content = src.read_text()
        data = {"name": "users", "kind": "model"}
        cache.put(src, tmp_project, data)

        # Rewrite same content (simulates git checkout) — different mtime
        time.sleep(0.01)
        src.write_text(original_content)

        result = cache.get(src, tmp_project)
        assert result == data

    def test_corrupted_cache_file(self, cache: CompiledCache, tmp_project: Path):
        src = tmp_project / "models" / "users.json"
        data = {"name": "users"}
        cache.put(src, tmp_project, data)

        # Corrupt the cache file
        cache_path = cache._cache_path(src, tmp_project)
        cache_path.write_text("not valid json{{{")

        result = cache.get(src, tmp_project)
        assert result is None

    def test_clean_removes_orphans(self, cache: CompiledCache, tmp_project: Path):
        src = tmp_project / "models" / "users.json"
        cache.put(src, tmp_project, {"name": "users"})

        # Delete source file
        src.unlink()

        removed = cache.clean(tmp_project)
        assert removed == 1

    def test_clean_keeps_valid(self, cache: CompiledCache, tmp_project: Path):
        src = tmp_project / "models" / "users.json"
        cache.put(src, tmp_project, {"name": "users"})

        removed = cache.clean(tmp_project)
        assert removed == 0

    def test_source_deleted_returns_none(self, cache: CompiledCache, tmp_project: Path):
        src = tmp_project / "models" / "users.json"
        cache.put(src, tmp_project, {"name": "users"})

        src.unlink()
        result = cache.get(src, tmp_project)
        assert result is None
