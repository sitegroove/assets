#!/usr/bin/env python3
"""Demo 4: Custom Loader — loading assets from YAML service manifests.

Shows how consumers own file parsing via the ``Loader`` protocol, while
the library handles discovery, index diffing, state rehydration, and
registration through :meth:`FileDiscovery.load`.

The domain is **service manifests**: each YAML file describes a
microservice with its metadata and dependencies.  No SQL involved.

What you will learn:
  - The Loader protocol (consumer-owned parsing)
  - SourceGroup configuration (root, patterns, loader)
  - FileDiscovery.load() — the library's discovery + indexing pipeline
  - How the file cache works (second load is fast)

Run: pip install pyyaml && python demos/04_custom_loader/main.py
"""

import shutil
import tempfile
from pathlib import Path
from typing import Any

import yaml

from assets import (
    Asset,
    LoadedAsset,
    Project,
    SourceGroup,
)

# ──────────────────────────────────────────────────────────────
# 1. Define asset types
# ──────────────────────────────────────────────────────────────


class Service(Asset):
    """A microservice described in a YAML manifest."""

    owner: str = ""
    language: str = "python"
    port: int = 8080


# ──────────────────────────────────────────────────────────────
# 2. Consumer-owned loader
# ──────────────────────────────────────────────────────────────


class YamlServiceLoader:
    """Parses YAML service manifests.

    Implements the ``Loader`` protocol expected by ``SourceGroup``.
    The library never looks inside the files — it delegates to this
    loader for all parsing decisions.
    """

    def load(self, path: Path, root: Path) -> list[LoadedAsset]:
        """Parse a single YAML manifest and return a LoadedAsset."""
        data = self._parse(path)
        if not data:
            return []
        asset = Service.model_validate(data)
        return [LoadedAsset(asset=asset, deps=[])]

    @staticmethod
    def _parse(path: Path) -> dict[str, Any]:
        data = yaml.safe_load(path.read_text())
        if not isinstance(data, dict) or "id" not in data:
            return {}
        return data


# ──────────────────────────────────────────────────────────────
# 3. Create sample YAML manifests in a temp directory
# ──────────────────────────────────────────────────────────────

tmpdir = tempfile.mkdtemp(prefix="assets_loader_demo_")
manifests_dir = Path(tmpdir) / "services"
manifests_dir.mkdir()

print(f"Project directory: {tmpdir}\n")

# Write service manifests
(manifests_dir / "user_service.yaml").write_text(
    yaml.dump(
        {
            "id": "user-service",
            "type": "backend",
            "tags": ["core", "auth"],
            "owner": "team-identity",
            "language": "python",
            "port": 8001,
        }
    )
)

(manifests_dir / "order_service.yaml").write_text(
    yaml.dump(
        {
            "id": "order-service",
            "type": "backend",
            "tags": ["core", "commerce"],
            "owner": "team-commerce",
            "language": "go",
            "port": 8002,
            "depends_on": ["user-service"],
        }
    )
)

(manifests_dir / "web_frontend.yaml").write_text(
    yaml.dump(
        {
            "id": "web-frontend",
            "type": "frontend",
            "tags": ["web"],
            "owner": "team-web",
            "language": "typescript",
            "port": 3000,
            "depends_on": ["user-service", "order-service"],
        }
    )
)

# ──────────────────────────────────────────────────────────────
# 4. Set up the project with FileDiscovery
# ──────────────────────────────────────────────────────────────

project = Project(environment="production", state_dir=tmpdir + "/.state")

groups = [
    SourceGroup(
        name="services",
        root=Path(tmpdir),
        directory=manifests_dir,
        patterns=["*.yaml"],
        loader=YamlServiceLoader(),
    ),
]

# ──────────────────────────────────────────────────────────────
# 5. First load — parses all YAML files
# ──────────────────────────────────────────────────────────────

print("=== First Load (from files) ===\n")
result = project.load(groups)
print(result.summary())

print(f"\nRegistered {len(project)} assets:")
for asset in project.all():
    print(f"  {asset.id} (type={asset.type}, depends_on={asset.depends_on})")

# Plan and apply so state is persisted
plan = project.plan()
print(f"\n{plan.show()}")
apply_result = project.apply(plan)
print(f"Applied: created={apply_result.created}")

# ──────────────────────────────────────────────────────────────
# 6. Second load — hits file cache + state (fast path)
# ──────────────────────────────────────────────────────────────

print("\n=== Second Load (from state) ===\n")
project.clear()
result2 = project.load(groups)
print(result2.summary())

print(f"\nStill {len(project)} assets (loaded from cache/state)")

# ──────────────────────────────────────────────────────────────
# 7. Modify a file and reload — only the changed file is re-parsed
# ──────────────────────────────────────────────────────────────

print("\n=== Third Load (after modifying order-service) ===\n")

(manifests_dir / "order_service.yaml").write_text(
    yaml.dump(
        {
            "id": "order-service",
            "type": "backend",
            "tags": ["core", "commerce", "v2"],  # added tag
            "owner": "team-commerce",
            "language": "go",
            "port": 8002,
            "depends_on": ["user-service"],
        }
    )
)

project.clear()
result3 = project.load(groups)
print(result3.summary())

plan3 = project.plan()
print(f"\n{plan3.show()}")

# ──────────────────────────────────────────────────────────────
# 8. Graph exploration
# ──────────────────────────────────────────────────────────────

print("\n=== Graph ===\n")
graph = project.graph
print(f"Roots:  {graph.roots()}")
print(f"Leaves: {graph.leaves()}")
print(f"Topological order: {graph.topological_sort()}")

# Cleanup
shutil.rmtree(tmpdir)
print(f"\nCleaned up {tmpdir}")
