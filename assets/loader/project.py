"""ProjectLoader — loads asset definition files and registers them.

This is a general-purpose loader. Consumers can subclass to add
support for specific file formats (YAML, Python, etc.).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import BaseModel

from assets.core.asset import Asset
from assets.core.registry import Registry
from assets.loader.compiled import CompiledCache


class LoadError(BaseModel):
    """A single load error."""

    path: str
    error: str
    error_type: str = ""


class LoadResult(BaseModel):
    """Summary of a load operation."""

    loaded: int
    reused: int = 0
    recompiled: int = 0
    errors: list[LoadError] = []


class ProjectLoader:
    """Base project loader. Loads asset dicts and registers them.

    Consumers subclass this to implement `parse_file()` for their
    specific file format (YAML, Python, TOML, etc.).
    """

    def __init__(
        self,
        registry: Registry,
        asset_class: type[Asset] = Asset,
        cache_dir: str = ".assets_state/compiled",
    ) -> None:
        self.registry = registry
        self.asset_class = asset_class
        self.cache = CompiledCache(cache_dir)

    def parse_file(self, path: Path, root: Path) -> dict[str, Any] | None:
        """Parse a single source file into an asset dict.

        Override in subclasses for specific formats.
        Default implementation reads JSON files.
        Returns None if the file cannot be parsed.
        """
        import json

        if path.suffix == ".json":
            return json.loads(path.read_text())
        return None

    def discover_files(self, project_dir: Path) -> list[Path]:
        """Discover asset definition files in the project directory.

        Override in subclasses to customize file discovery.
        Default finds .json files.
        """
        return sorted(project_dir.rglob("*.json"))

    def load(self, project_dir: str) -> LoadResult:
        """Load all asset files, registering assets into registry.

        Uses compiled cache for fast cold starts.
        """
        root = Path(project_dir)
        if not root.exists():
            return LoadResult(loaded=0, errors=[LoadError(path=project_dir, error="not found", error_type="not_found")])

        files = self.discover_files(root)
        loaded = 0
        reused = 0
        recompiled = 0
        errors: list[LoadError] = []

        for path in files:
            try:
                # Try compiled cache first
                cached = self.cache.get(path, root)
                if cached is not None:
                    asset = self.asset_class.model_validate(cached)
                    self.registry.register(asset)
                    loaded += 1
                    reused += 1
                    continue

                # Parse from source
                data = self.parse_file(path, root)
                if data is None:
                    continue

                asset = self.asset_class.model_validate(data)
                self.registry.register(asset)
                self.cache.put(path, root, data)
                loaded += 1
                recompiled += 1

            except Exception as e:
                errors.append(LoadError(path=str(path), error=str(e), error_type=type(e).__name__))

        return LoadResult(loaded=loaded, reused=reused, recompiled=recompiled, errors=errors)

    def load_specific(self, paths: list[Path], root: Path) -> list[Asset]:
        """Load only specific files (for optimized plan)."""
        assets: list[Asset] = []
        for path in paths:
            cached = self.cache.get(path, root)
            if cached is not None:
                asset = self.asset_class.model_validate(cached)
            else:
                data = self.parse_file(path, root)
                if data is None:
                    continue
                asset = self.asset_class.model_validate(data)
                self.cache.put(path, root, data)
            self.registry.register(asset)
            assets.append(asset)
        return assets
