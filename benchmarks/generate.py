"""Synthetic model generator for benchmarking.

Generates N assets with realistic properties and configurable
dependency fanout.  Used by both pytest-benchmark tests and the
standalone end-to-end script.
"""

from __future__ import annotations

import random

from assets.core.asset import Asset
from assets.core.dependency import Dependency

# Reproducible randomness
_RNG = random.Random(42)

_TYPES = ["source", "staging", "intermediate", "mart", "exposure"]
_TAGS_POOL = [
    "pii",
    "finance",
    "marketing",
    "tier1",
    "tier2",
    "daily",
    "hourly",
    "deprecated",
    "critical",
    "experimental",
]


def generate_assets(
    n: int,
    *,
    max_deps: int = 5,
    tag_count: int = 2,
    child_count: int = 3,
    seed: int = 42,
) -> tuple[list[Asset], list[Dependency]]:
    """Generate N assets with realistic dependency graph.

    Args:
        n: Number of assets to generate.
        max_deps: Maximum upstream dependencies per asset (1 to max_deps).
        tag_count: Number of tags per asset.
        child_count: Number of children (columns) per asset.
        seed: Random seed for reproducibility.

    Returns:
        Tuple of (assets, dependencies).
    """
    rng = random.Random(seed)
    assets: list[Asset] = []
    dependencies: list[Dependency] = []

    for i in range(n):
        asset_type = _TYPES[i % len(_TYPES)]
        tags = rng.sample(_TAGS_POOL, min(tag_count, len(_TAGS_POOL)))

        # Dependencies: only depend on assets with lower index (DAG guarantee)
        dep_ids: list[str] = []
        if i > 0:
            num_deps = rng.randint(1, min(max_deps, i))
            dep_indices = rng.sample(range(i), num_deps)
            dep_ids = [f"model_{j:05d}" for j in dep_indices]

        children = [
            Asset(id=f"col_{k}", type="column", description=f"Column {k}")
            for k in range(child_count)
        ]

        asset = Asset(
            id=f"model_{i:05d}",
            type=asset_type,
            description=f"Synthetic model {i} for benchmarking",
            depends_on=dep_ids,
            sql=f"SELECT * FROM upstream_{i}",
            tags=tags,
            metadata={"batch": i // 100, "priority": rng.randint(1, 5)},
            children=children,
        )
        assets.append(asset)

        for dep_id in dep_ids:
            dependencies.append(Dependency(source=dep_id, target=asset.id, type="ref"))

    return assets, dependencies
