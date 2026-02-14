from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

from assets import (
    Asset,
    LoadedAsset,
)


class ResourcesLoader:
    """Imports Python config modules and extracts Asset instances.

    This loader is consumer-owned — the library never prescribes
    how files are parsed.

    Args:
        deps: Relative paths (from root) of files this group depends
            on (e.g. model definitions, shared defaults).
        base_class: Only collect instances of this class.
            Defaults to ``Asset``.
    """

    def __init__(
        self,
        deps: list[str] | None = None,
        base_class: type[Asset] | None = None,
    ) -> None:
        self._deps = deps or []
        self._base_class = base_class or Asset

    def load(self, path: Path, root: Path) -> list[LoadedAsset]:
        """Import a Python module and extract Asset instances."""
        assets = self._import_and_extract(path, root)
        dep_pairs: list[tuple[Path, str]] = [
            (root / d, d.split(".")[0]) for d in self._deps
        ]
        return [LoadedAsset(asset=a, deps=dep_pairs) for a in assets]

    def _import_and_extract(self, module_path: Path, project_root: Path) -> list[Asset]:
        """Import a module and return all locally-defined Asset instances."""
        base_class = self._base_class

        module_name = f"gcp_cfg_{module_path.stem}"
        spec = importlib.util.spec_from_file_location(module_name, module_path)
        if spec is None or spec.loader is None:
            raise ValueError(f"Could not load module spec for: {module_path}")

        module = importlib.util.module_from_spec(spec)
        if str(project_root) not in sys.path:
            sys.path.insert(0, str(project_root))
        spec.loader.exec_module(module)

        # Skip re-imported assets from other modules
        imported_ids: set[int] = {
            id(v)
            for mod in sys.modules.values()
            if mod is not None and mod is not module
            for v in vars(mod).values()
            if isinstance(v, base_class)
        }

        assets: list[Asset] = [
            obj
            for obj in vars(module).values()
            if isinstance(obj, base_class) and obj.id and id(obj) not in imported_ids
        ]

        if not assets:
            raise ValueError(
                f"No {base_class.__name__} instances with id found in: {module_path}"
            )
        return assets
