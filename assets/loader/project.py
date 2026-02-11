"""ProjectLoader — loads asset definition files and registers them.

This is a general-purpose loader. Consumers can subclass to add
support for specific file formats (YAML, Python, etc.).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import BaseModel

from pydantic import BaseModel as PydanticBaseModel

from assets.core.asset import Asset
from assets.core.registry import Registry
from assets.loader.compiled import CompiledCache, CompiledEntry
from assets.resolver.ref import RefResolver


class LoadError(BaseModel):
    """A single load error."""

    path: str
    error: str


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

    def _extract_refs(self, asset: Asset) -> list[str]:
        """Extract refs from an asset's SQL, filtering self-references."""
        if not asset.sql:
            return []
        refs = RefResolver().extract_refs(asset.sql)
        return [ref for ref in refs if ref != asset.name]

    def _construct_from_cache(self, entry: CompiledEntry) -> Asset:
        """Build an Asset from a v2 cache entry without full Pydantic validation.

        Uses model_construct() since the data was already validated when
        first cached. Nested Pydantic models (e.g. Column lists) are
        constructed recursively to preserve attribute access.
        Sets _cached_fingerprint to skip SHA-256 recomputation.
        """
        data = dict(entry.data)
        if entry.depends_on is not None:
            data["depends_on"] = entry.depends_on

        # Construct nested Pydantic models from raw dicts
        self._construct_nested_fields(self.asset_class, data)

        asset = self.asset_class.model_construct(**data)
        if entry.fingerprint is not None:
            asset._cached_fingerprint = entry.fingerprint
        return asset

    @staticmethod
    def _construct_nested_fields(cls: type, data: dict[str, Any]) -> None:
        """Recursively construct nested Pydantic models in field data."""
        for field_name, field_info in cls.model_fields.items():
            if field_name not in data:
                continue
            annotation = field_info.annotation
            # Handle list[SomeModel] fields
            origin = getattr(annotation, "__origin__", None)
            if origin is list:
                args = getattr(annotation, "__args__", ())
                if args and isinstance(args[0], type) and issubclass(args[0], PydanticBaseModel):
                    inner_cls = args[0]
                    data[field_name] = [
                        inner_cls.model_construct(**item) if isinstance(item, dict) else item
                        for item in data[field_name]
                    ]

    def load(self, project_dir: str) -> LoadResult:
        """Load all asset files, registering assets into registry.

        Uses compiled cache for fast cold starts. On v2 cache hits,
        skips Pydantic validation and uses pre-computed fingerprints/refs
        for much faster warm loads.
        """
        root = Path(project_dir)
        if not root.exists():
            return LoadResult(loaded=0, errors=[LoadError(path=project_dir, error="not found")])

        files = self.discover_files(root)
        loaded = 0
        reused = 0
        recompiled = 0
        errors: list[LoadError] = []

        # Collect assets for bulk registration
        bulk_assets: list[Asset] = []
        bulk_refs: list[list[str] | None] = []

        for path in files:
            try:
                # Try compiled cache first
                entry = self.cache.get(path, root)
                if entry is not None:
                    if entry.version >= 2 and entry.fingerprint is not None:
                        # Fast path: v2 cache — skip validation + recomputation
                        asset = self._construct_from_cache(entry)
                        bulk_assets.append(asset)
                        bulk_refs.append(entry.refs)
                    else:
                        # v1 cache — only raw dict available
                        asset = self.asset_class.model_validate(entry.data)
                        bulk_assets.append(asset)
                        bulk_refs.append(None)
                    loaded += 1
                    reused += 1
                    continue

                # Parse from source
                data = self.parse_file(path, root)
                if data is None:
                    continue

                asset = self.asset_class.model_validate(data)
                refs = self._extract_refs(asset)

                # Set depends_on before fingerprint so the cached fingerprint
                # matches what register_bulk() will produce.
                if refs:
                    asset.depends_on = refs

                self.cache.put(
                    path, root, data,
                    fingerprint=asset.fingerprint,
                    refs=refs,
                    depends_on=refs,
                )
                bulk_assets.append(asset)
                bulk_refs.append(refs)
                loaded += 1
                recompiled += 1

            except Exception as e:
                errors.append(LoadError(path=str(path), error=str(e)))

        # Bulk register all assets at once (deferred cycle detection)
        if bulk_assets:
            self.registry.register_bulk(bulk_assets, bulk_refs)

        return LoadResult(loaded=loaded, reused=reused, recompiled=recompiled, errors=errors)

    def load_specific(self, paths: list[Path], root: Path) -> list[Asset]:
        """Load only specific files (for optimized plan)."""
        assets: list[Asset] = []
        bulk_assets: list[Asset] = []
        bulk_refs: list[list[str] | None] = []

        for path in paths:
            entry = self.cache.get(path, root)
            if entry is not None:
                if entry.version >= 2 and entry.fingerprint is not None:
                    asset = self._construct_from_cache(entry)
                    bulk_assets.append(asset)
                    bulk_refs.append(entry.refs)
                else:
                    asset = self.asset_class.model_validate(entry.data)
                    bulk_assets.append(asset)
                    bulk_refs.append(None)
            else:
                data = self.parse_file(path, root)
                if data is None:
                    continue
                asset = self.asset_class.model_validate(data)
                refs = self._extract_refs(asset)
                if refs:
                    asset.depends_on = refs
                self.cache.put(
                    path, root, data,
                    fingerprint=asset.fingerprint,
                    refs=refs,
                    depends_on=refs,
                )
                bulk_assets.append(asset)
                bulk_refs.append(refs)
            assets.append(asset)

        if bulk_assets:
            self.registry.register_bulk(bulk_assets, bulk_refs)

        return assets
