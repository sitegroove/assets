#!/usr/bin/env python3
"""Demo 7: Terraform-style Python configuration project.

This demo shows a realistic project layout where assets are declared across
multiple Python files as Pydantic models (configuration only).

No runtime "resource creation" logic lives in the config files. They only
declare metadata and lineage. The library's :meth:`FileDiscovery.load`
handles discovery, file caching, state reuse, and registration.  The
consumer-provided ``TerraformConfigLoader`` handles Python module import.

Run:
    python demos/07_terraform_style_python_configs/main.py
"""

from __future__ import annotations

import importlib.util
import shutil
import sys
from pathlib import Path

from assets import Asset, Assets, LoadedAsset, SourceGroup


# ── Consumer-owned loader ────────────────────────────────────


class TerraformConfigLoader:
    """Imports Python config modules and extracts declared assets.

    Supported module shapes:
    - ``ASSET = DataModel(...)``
    - ``ASSETS = [DataModel(...), ...]``
    """

    def __init__(self, deps: list[str] | None = None) -> None:
        self._deps = deps or []

    def load(self, path: Path, root: Path) -> list[LoadedAsset]:
        """Import a config module and return declared assets."""
        assets = self._import_and_extract(path, root)
        dep_pairs: list[tuple[Path, str]] = [
            (root / d, d.split(".")[0]) for d in self._deps
        ]
        return [LoadedAsset(asset=a, deps=dep_pairs) for a in assets]

    def _import_and_extract(self, module_path: Path, project_root: Path) -> list[Asset]:
        module_name = f"asset_cfg_{module_path.stem}"
        spec = importlib.util.spec_from_file_location(module_name, module_path)
        if spec is None or spec.loader is None:
            raise ValueError(f"Could not load module spec for: {module_path}")

        module = importlib.util.module_from_spec(spec)
        if str(project_root) not in sys.path:
            sys.path.insert(0, str(project_root))
        spec.loader.exec_module(module)

        single = getattr(module, "ASSET", None)
        if single is not None:
            if not isinstance(single, Asset):
                raise ValueError(f"ASSET must be an Asset instance: {module_path}")
            return [single]

        assets = getattr(module, "ASSETS", None)
        if isinstance(assets, list):
            if not all(isinstance(a, Asset) for a in assets):
                raise ValueError(
                    f"ASSETS must contain only Asset instances: {module_path}"
                )
            return assets

        raise ValueError(f"Module must define ASSET or ASSETS: {module_path}")


# ── Main ─────────────────────────────────────────────────────


def main() -> None:
    project_root = Path(__file__).parent / "terraform_style_project"
    state_dir = project_root / ".assets_state"
    shutil.rmtree(state_dir, ignore_errors=True)

    project = Assets(environment="production", state_dir=str(state_dir))

    groups = [
        SourceGroup(
            name="resources",
            root=project_root,
            directory=project_root / "resources",
            patterns=["*.py"],
            exclude=["__init__.py"],
            loader=TerraformConfigLoader(
                deps=["project_models.py"],
            ),
        ),
    ]

    print("=" * 70)
    print("Terraform-Style Python Config Demo")
    print("=" * 70)
    print(f"Project root: {project_root}")

    print("\n[1] First load (parses config files)")
    result = project.load(groups)
    print(result.summary())

    plan = project.plan()
    print("\n[2] Plan")
    print(plan.show())

    apply_result = project.apply(plan)
    print(f"Applied: created={apply_result.created}, updated={apply_result.updated}")

    print("\n[3] Second load (hits state + file cache)")
    project.clear()
    result2 = project.load(groups)
    print(result2.summary())

    graph = project.graph
    print("\n[4] Lineage checks")
    print(f"Roots: {graph.roots()}")
    print(f"Leaves: {graph.leaves()}")
    print(f"Downstream of raw.orders: {graph.descendants('raw.orders')}")


if __name__ == "__main__":
    main()
