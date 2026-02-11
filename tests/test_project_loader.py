"""Tests for ProjectLoader."""

import json
from pathlib import Path

import pytest

from assets import ProjectLoader, Registry


@pytest.fixture
def project_dir(tmp_path: Path) -> Path:
    """Create a temp project with JSON asset files."""
    models = tmp_path / "models"
    models.mkdir()

    (models / "users.json").write_text(
        json.dumps({"name": "raw.users", "kind": "source", "tags": ["raw"]})
    )
    (models / "orders.json").write_text(
        json.dumps({"name": "raw.orders", "kind": "source", "tags": ["raw"]})
    )
    return tmp_path


class TestProjectLoader:
    def test_load_json_files(self, project_dir: Path):
        registry = Registry()
        loader = ProjectLoader(
            registry, cache_dir=str(project_dir / ".cache")
        )
        result = loader.load(str(project_dir / "models"))
        assert result.loaded == 2
        assert result.recompiled == 2
        assert result.errors == []
        assert registry.get("raw.users") is not None

    def test_load_uses_cache(self, project_dir: Path):
        registry = Registry()
        loader = ProjectLoader(
            registry, cache_dir=str(project_dir / ".cache")
        )
        # First load
        loader.load(str(project_dir / "models"))

        # Second load — should hit cache
        registry.clear()
        result = loader.load(str(project_dir / "models"))
        assert result.loaded == 2
        assert result.reused == 2
        assert result.recompiled == 0

    def test_load_nonexistent_dir(self):
        registry = Registry()
        loader = ProjectLoader(registry)
        result = loader.load("/nonexistent/path")
        assert result.loaded == 0
        assert len(result.errors) == 1

    def test_load_invalid_file(self, project_dir: Path):
        (project_dir / "models" / "bad.json").write_text("not valid json{{{")
        registry = Registry()
        loader = ProjectLoader(
            registry, cache_dir=str(project_dir / ".cache")
        )
        result = loader.load(str(project_dir / "models"))
        assert result.loaded == 2  # the 2 valid files
        assert len(result.errors) == 1

    def test_load_specific(self, project_dir: Path):
        registry = Registry()
        loader = ProjectLoader(
            registry, cache_dir=str(project_dir / ".cache")
        )
        models_dir = project_dir / "models"
        paths = [models_dir / "users.json"]
        assets = loader.load_specific(paths, models_dir)
        assert len(assets) == 1
        assert assets[0].name == "raw.users"

    def test_sql_ref_extraction_on_load(self, project_dir: Path):
        (project_dir / "models" / "staging.json").write_text(
            json.dumps({
                "name": "staging.users",
                "kind": "data_model",
                "sql": "SELECT * FROM {{ ref('raw.users') }}",
            })
        )
        registry = Registry()
        loader = ProjectLoader(
            registry, cache_dir=str(project_dir / ".cache")
        )
        loader.load(str(project_dir / "models"))

        asset = registry.get("staging.users")
        assert asset is not None
        assert asset.depends_on == ["raw.users"]
