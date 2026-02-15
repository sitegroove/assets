"""Tests for FileDiscovery."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

from assets import Asset, Registry, StateManager
from assets.loader.discovery import (
    FileDiscovery,
    GroupLoadResult,
    LoadedAsset,
    Loader,
    LoadResult,
    SourceGroup,
)


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """Create a multi-directory project layout."""
    for sub in ("raw", "staging", "marts"):
        d = tmp_path / "models" / sub
        d.mkdir(parents=True)
    (tmp_path / "models" / "raw" / "users.yaml").write_text("{}")
    (tmp_path / "models" / "raw" / "orders.yaml").write_text("{}")
    (tmp_path / "models" / "raw" / "users.sql").write_text("SELECT 1")
    (tmp_path / "models" / "staging" / "stg_users.yaml").write_text("{}")
    (tmp_path / "models" / "marts" / "revenue.yaml").write_text("{}")
    (tmp_path / "models" / "marts" / "revenue.json").write_text("{}")
    return tmp_path


class TestFileDiscovery:
    def test_single_group(self, project: Path) -> None:
        discovery = FileDiscovery(
            groups=[
                SourceGroup(directory=project / "models" / "raw"),
            ]
        )
        result = discovery.discover()
        names = {f.path.name for f in result.files}
        assert names == {"users.yaml", "orders.yaml"}

    def test_multiple_groups(self, project: Path) -> None:
        discovery = FileDiscovery(
            groups=[
                SourceGroup(directory=project / "models" / "raw"),
                SourceGroup(directory=project / "models" / "staging"),
            ]
        )
        result = discovery.discover()
        assert len(result.files) == 3

    def test_multiple_patterns(self, project: Path) -> None:
        discovery = FileDiscovery(
            groups=[
                SourceGroup(
                    directory=project / "models" / "marts",
                    patterns=["*.yaml", "*.json"],
                ),
            ]
        )
        result = discovery.discover()
        names = {f.path.name for f in result.files}
        assert names == {"revenue.yaml", "revenue.json"}

    def test_exclude_pattern(self, project: Path) -> None:
        discovery = FileDiscovery(
            groups=[
                SourceGroup(
                    directory=project / "models" / "raw",
                    exclude=["*orders*"],
                ),
            ]
        )
        result = discovery.discover()
        names = {f.path.name for f in result.files}
        assert "orders.yaml" not in names
        assert "users.yaml" in names

    def test_name_regex_filter(self, project: Path) -> None:
        import re

        discovery = FileDiscovery(
            groups=[
                SourceGroup(
                    directory=project / "models" / "staging",
                    name_regex=re.compile(r"^stg_"),
                ),
            ]
        )
        result = discovery.discover()
        assert len(result.files) == 1
        assert result.files[0].path.name == "stg_users.yaml"

    def test_recursive_scan(self, project: Path) -> None:
        """Scan the top-level models/ dir — should find files recursively."""
        discovery = FileDiscovery(
            groups=[
                SourceGroup(directory=project / "models"),
            ]
        )
        result = discovery.discover()
        names = {f.path.name for f in result.files}
        assert "users.yaml" in names
        assert "stg_users.yaml" in names
        assert "revenue.yaml" in names

    def test_deduplicates_across_groups(self, project: Path) -> None:
        discovery = FileDiscovery(
            groups=[
                SourceGroup(directory=project / "models" / "raw"),
                SourceGroup(directory=project / "models" / "raw"),
            ]
        )
        result = discovery.discover()
        paths = [f.path.name for f in result.files]
        assert paths.count("users.yaml") == 1

    def test_mtime_collected(self, project: Path) -> None:
        discovery = FileDiscovery(
            groups=[
                SourceGroup(directory=project / "models" / "raw"),
            ]
        )
        result = discovery.discover()
        for f in result.files:
            assert f.mtime_ns > 0

    def test_empty_directory(self, tmp_path: Path) -> None:
        empty = tmp_path / "empty"
        empty.mkdir()
        discovery = FileDiscovery(
            groups=[
                SourceGroup(directory=empty),
            ]
        )
        result = discovery.discover()
        assert result.files == []

    def test_nonexistent_directory_skipped(self, tmp_path: Path) -> None:
        discovery = FileDiscovery(
            groups=[
                SourceGroup(directory=tmp_path / "does_not_exist"),
            ]
        )
        result = discovery.discover()
        assert result.files == []

    def test_sorted_output(self, project: Path) -> None:
        discovery = FileDiscovery(
            groups=[
                SourceGroup(directory=project / "models"),
            ]
        )
        result = discovery.discover()
        paths = [f.path for f in result.files]
        assert paths == sorted(paths)


# ── Loader protocol & FileDiscovery.load() tests ────────────


class SimpleYamlLoader:
    """Test loader: reads files and creates assets with id = stem."""

    def __init__(self, deps: list[str] | None = None) -> None:
        self._deps = deps or []

    def load(self, path: Path, root: Path) -> list[LoadedAsset]:
        asset = Asset(id=path.stem, type="test")
        dep_pairs = [(root / d, d) for d in self._deps]
        return [LoadedAsset(asset=asset, deps=dep_pairs)]


class PackageLoader:
    """Imports a fixed package module from each source root."""

    def __init__(self, module_name: str) -> None:
        self._module_name = module_name

    def load(self, path: Path, root: Path) -> list[LoadedAsset]:
        if str(root) not in sys.path:
            sys.path.insert(0, str(root))
        importlib.invalidate_caches()
        module = importlib.import_module(self._module_name)
        asset = getattr(module, "ASSET")
        return [LoadedAsset(asset=asset)]


class TestLoaderProtocol:
    def test_simple_loader_satisfies_protocol(self) -> None:
        loader = SimpleYamlLoader()
        assert isinstance(loader, Loader)

    def test_lambda_loader_does_not_satisfy_protocol(self) -> None:
        not_a_loader = lambda: None  # noqa: E731
        assert not isinstance(not_a_loader, Loader)


class TestLoadResult:
    def test_empty_result(self) -> None:
        result = LoadResult()
        assert result.loaded == 0
        assert result.from_state == 0
        assert result.parsed == 0
        assert result.deleted == 0

    def test_aggregate_totals(self) -> None:
        result = LoadResult(
            groups=[
                GroupLoadResult(group="a", loaded=3, from_state=1, parsed=2, deleted=0),
                GroupLoadResult(group="b", loaded=2, from_state=0, parsed=2, deleted=1),
            ]
        )
        assert result.loaded == 5
        assert result.from_state == 1
        assert result.parsed == 4
        assert result.deleted == 1

    def test_summary_output(self) -> None:
        result = LoadResult(
            groups=[
                GroupLoadResult(group="models", loaded=3, parsed=3),
            ]
        )
        summary = result.summary()
        assert "models" in summary
        assert "loaded=3" in summary
        assert "total:" in summary


class TestFileDiscoveryLoad:
    """Tests for FileDiscovery.load() orchestration."""

    @pytest.fixture
    def yaml_project(self, tmp_path: Path) -> Path:
        """Create a simple project with YAML-like files."""
        models = tmp_path / "models"
        models.mkdir()
        (models / "users.yaml").write_text("id: users")
        (models / "orders.yaml").write_text("id: orders")
        return tmp_path

    @pytest.fixture
    def manager(self, tmp_path: Path) -> StateManager:
        """Create a StateManager with a temp state directory."""
        state_dir = tmp_path / ".state"
        registry = Registry()
        return StateManager.create(registry, local_path=str(state_dir))

    def test_load_parses_all_files(
        self, yaml_project: Path, manager: StateManager
    ) -> None:
        discovery = FileDiscovery(
            groups=[
                SourceGroup(
                    name="models",
                    directory=yaml_project / "models",
                    loader=SimpleYamlLoader(),
                ),
            ]
        )
        result = discovery.load(manager, environment="production")

        assert result.loaded == 2
        assert result.parsed == 2
        assert result.from_state == 0
        assert len(result.groups) == 1
        assert result.groups[0].group == "models"

        # Assets registered in the registry
        assert manager.registry.get("users") is not None
        assert manager.registry.get("orders") is not None

    def test_load_rehydrates_from_state(
        self, yaml_project: Path, manager: StateManager
    ) -> None:
        """Second load after apply should rehydrate from state."""
        discovery = FileDiscovery(
            groups=[
                SourceGroup(
                    name="models",
                    directory=yaml_project / "models",
                    loader=SimpleYamlLoader(),
                ),
            ]
        )

        # First load + apply
        discovery.load(manager, environment="production")
        plan = manager.plan(environment="production")
        manager.apply(plan, environment="production")

        # Clear registry, second load
        manager.registry.clear()
        result = discovery.load(manager, environment="production")

        assert result.loaded == 2
        assert result.from_state == 2
        assert result.parsed == 0

        # Assets still in registry
        assert manager.registry.get("users") is not None
        assert manager.registry.get("orders") is not None

    def test_load_detects_stale_after_modification(
        self, yaml_project: Path, manager: StateManager
    ) -> None:
        """Modified files should be re-parsed, not loaded from state."""
        discovery = FileDiscovery(
            groups=[
                SourceGroup(
                    name="models",
                    directory=yaml_project / "models",
                    loader=SimpleYamlLoader(),
                ),
            ]
        )

        # First load + apply
        discovery.load(manager, environment="production")
        plan = manager.plan(environment="production")
        manager.apply(plan, environment="production")

        # Modify a file (change content + touch mtime)
        users_file = yaml_project / "models" / "users.yaml"
        users_file.write_text("id: users\nchanged: true")

        # Clear and reload
        manager.registry.clear()
        result = discovery.load(manager, environment="production")

        # One file should be stale (re-parsed), one from state
        assert result.loaded == 2
        assert result.parsed >= 1

    def test_load_handles_deletions(
        self, yaml_project: Path, manager: StateManager
    ) -> None:
        """Deleted files should be unregistered."""
        discovery = FileDiscovery(
            groups=[
                SourceGroup(
                    name="models",
                    directory=yaml_project / "models",
                    loader=SimpleYamlLoader(),
                ),
            ]
        )

        # First load + apply
        discovery.load(manager, environment="production")
        plan = manager.plan(environment="production")
        manager.apply(plan, environment="production")

        # Delete a file
        (yaml_project / "models" / "orders.yaml").unlink()

        # Clear and reload
        manager.registry.clear()
        result = discovery.load(manager, environment="production")

        assert result.deleted == 1
        assert result.loaded == 1
        assert manager.registry.get("orders") is None
        assert manager.registry.get("users") is not None

    def test_load_multi_group(self, tmp_path: Path, manager: StateManager) -> None:
        """Load from multiple groups with isolation."""
        group_a = tmp_path / "group_a"
        group_a.mkdir()
        (group_a / "alpha.yaml").write_text("id: alpha")

        group_b = tmp_path / "group_b"
        group_b.mkdir()
        (group_b / "beta.yaml").write_text("id: beta")

        discovery = FileDiscovery(
            groups=[
                SourceGroup(
                    name="a",
                    directory=group_a,
                    loader=SimpleYamlLoader(),
                ),
                SourceGroup(
                    name="b",
                    directory=group_b,
                    loader=SimpleYamlLoader(),
                ),
            ]
        )
        result = discovery.load(manager, environment="production")

        assert len(result.groups) == 2
        assert result.loaded == 2
        assert manager.registry.get("alpha") is not None
        assert manager.registry.get("beta") is not None

    def test_load_with_deps(self, yaml_project: Path, manager: StateManager) -> None:
        """Loader can declare file dependencies."""
        # Create the dep file so FileIndex can hash it
        schema_file = yaml_project / "models" / "schema.py"
        schema_file.write_text("# schema")

        discovery = FileDiscovery(
            groups=[
                SourceGroup(
                    name="models",
                    directory=yaml_project / "models",
                    patterns=["*.yaml"],
                    loader=SimpleYamlLoader(deps=["schema.py"]),
                ),
            ]
        )
        result = discovery.load(manager, environment="production")
        assert result.loaded == 2

    def test_load_raises_without_loader(
        self, yaml_project: Path, manager: StateManager
    ) -> None:
        """Groups without a loader should raise ValueError."""
        discovery = FileDiscovery(
            groups=[
                SourceGroup(
                    name="models",
                    directory=yaml_project / "models",
                ),
            ]
        )
        with pytest.raises(ValueError, match="has no loader"):
            discovery.load(manager, environment="production")

    def test_load_raises_without_name(
        self, yaml_project: Path, manager: StateManager
    ) -> None:
        """Groups without a name should raise ValueError."""
        discovery = FileDiscovery(
            groups=[
                SourceGroup(
                    directory=yaml_project / "models",
                    loader=SimpleYamlLoader(),
                ),
            ]
        )
        with pytest.raises(ValueError, match="has no name"):
            discovery.load(manager, environment="production")

    def test_load_root_defaults_to_directory(
        self, yaml_project: Path, manager: StateManager
    ) -> None:
        """When root is not set, it defaults to directory."""
        discovery = FileDiscovery(
            groups=[
                SourceGroup(
                    name="models",
                    directory=yaml_project / "models",
                    loader=SimpleYamlLoader(),
                ),
            ]
        )
        result = discovery.load(manager, environment="production")
        assert result.loaded == 2

    def test_load_custom_root(self, yaml_project: Path, manager: StateManager) -> None:
        """Custom root is passed through to loader and index."""
        discovery = FileDiscovery(
            groups=[
                SourceGroup(
                    name="models",
                    root=yaml_project,
                    directory=yaml_project / "models",
                    loader=SimpleYamlLoader(),
                ),
            ]
        )
        result = discovery.load(manager, environment="production")
        assert result.loaded == 2

    def test_load_runs_setup_and_teardown(
        self, yaml_project: Path, manager: StateManager
    ) -> None:
        events: list[str] = []

        discovery = FileDiscovery(
            groups=[
                SourceGroup(
                    name="models",
                    directory=yaml_project / "models",
                    loader=SimpleYamlLoader(),
                    setup=lambda: events.append("setup"),
                    teardown=lambda: events.append("teardown"),
                ),
            ]
        )

        result = discovery.load(manager, environment="production")
        assert result.loaded == 2
        assert events == ["setup", "teardown"]

    def test_load_cleans_modules_between_roots(self, tmp_path: Path) -> None:
        """Same package name in two roots resolves correctly per group."""
        for name in list(sys.modules):
            if name == "resourcepkg" or name.startswith("resourcepkg."):
                del sys.modules[name]

        root_a = tmp_path / "root_a"
        root_b = tmp_path / "root_b"
        for root, asset_id in ((root_a, "asset.alpha"), (root_b, "asset.beta")):
            pkg = root / "resourcepkg"
            pkg.mkdir(parents=True)
            (pkg / "__init__.py").write_text("")
            (pkg / "asset_def.py").write_text(
                "from assets import Asset\n"
                f'ASSET = Asset(id="{asset_id}", type="test")\n'
            )

        registry = Registry()
        manager = StateManager.create(registry, local_path=str(tmp_path / ".state"))

        discovery = FileDiscovery(
            groups=[
                SourceGroup(
                    name="alpha",
                    root=root_a,
                    directory=root_a / "resourcepkg",
                    patterns=["asset_def.py"],
                    loader=PackageLoader("resourcepkg.asset_def"),
                ),
                SourceGroup(
                    name="beta",
                    root=root_b,
                    directory=root_b / "resourcepkg",
                    patterns=["asset_def.py"],
                    loader=PackageLoader("resourcepkg.asset_def"),
                ),
            ]
        )

        result = discovery.load(manager, environment="production")
        assert result.loaded == 2
        assert registry.get("asset.alpha") is not None
        assert registry.get("asset.beta") is not None

        for name in list(sys.modules):
            if name == "resourcepkg" or name.startswith("resourcepkg."):
                del sys.modules[name]
