"""Tests for CompiledCache."""

import json
import time
from pathlib import Path

import pytest

from assets import CompiledCache
from assets.loader.compiled import CompiledEntry


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
        assert result is not None
        assert result.data == data

    def test_mtime_fast_path(self, cache: CompiledCache, tmp_project: Path):
        src = tmp_project / "models" / "users.json"
        data = {"name": "users"}
        cache.put(src, tmp_project, data)

        # Same file, same mtime → cache hit
        result = cache.get(src, tmp_project)
        assert result is not None
        assert result.data == data

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
        assert result is not None
        assert result.data == data

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

    # --- v2 cache format tests ---

    def test_v2_entry_roundtrip(self, cache: CompiledCache, tmp_project: Path):
        """v2 put stores fingerprint/refs/depends_on; get returns them."""
        src = tmp_project / "models" / "users.json"
        data = {"name": "users", "kind": "model"}
        cache.put(
            src, tmp_project, data,
            fingerprint="abc123",
            refs=["raw.users"],
            depends_on=["raw.users"],
        )
        result = cache.get(src, tmp_project)
        assert result is not None
        assert result.version == 2
        assert result.fingerprint == "abc123"
        assert result.refs == ["raw.users"]
        assert result.depends_on == ["raw.users"]
        assert result.data == data

    def test_v1_entry_compat(self, cache: CompiledCache, tmp_project: Path):
        """Old v1 cache files (no version/fingerprint/refs) load with None defaults."""
        src = tmp_project / "models" / "users.json"
        # Manually write a v1-format entry (no version/fingerprint/refs fields)
        cp = cache._cache_path(src, tmp_project)
        cp.parent.mkdir(parents=True, exist_ok=True)
        v1_entry = {
            "source_path": str(src),
            "source_mtime": cache._mtime_ns(src),
            "content_hash": cache._content_hash(src),
            "data": {"name": "users", "kind": "model"},
        }
        cp.write_text(json.dumps(v1_entry))

        result = cache.get(src, tmp_project)
        assert result is not None
        assert result.version == 1
        assert result.fingerprint is None
        assert result.refs is None
        assert result.depends_on is None
        assert result.data == {"name": "users", "kind": "model"}

    def test_get_returns_compiled_entry(self, cache: CompiledCache, tmp_project: Path):
        """get() returns CompiledEntry, not a raw dict."""
        src = tmp_project / "models" / "users.json"
        cache.put(src, tmp_project, {"name": "users"})
        result = cache.get(src, tmp_project)
        assert isinstance(result, CompiledEntry)

    def test_v2_put_without_optional_fields(self, cache: CompiledCache, tmp_project: Path):
        """put() without v2 kwargs still produces a v2 entry (with None fields)."""
        src = tmp_project / "models" / "users.json"
        cache.put(src, tmp_project, {"name": "users"})
        result = cache.get(src, tmp_project)
        assert result is not None
        assert result.version == 2
        assert result.fingerprint is None
