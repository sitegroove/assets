"""Micro-benchmarks using pytest-benchmark.

Run:
    pytest tests/test_benchmarks.py -m benchmark --benchmark-enable
    pytest tests/test_benchmarks.py -m benchmark --benchmark-enable --benchmark-sort=mean

These tests are marked with ``@pytest.mark.benchmark`` and skipped
during normal ``pytest`` runs.  Use ``-m benchmark --benchmark-enable``
to run them.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from assets.core.asset import Asset
from assets.core.dependency import Dependency
from assets.core.graph import AssetGraph
from assets.core.registry import Registry
from assets.engine.differ import Differ
from assets.selector.parser import GraphSelector
from assets.state.models import AssetState, StateSnapshot
from assets.state.sqlite import SQLiteBackend

# Make benchmarks/ importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "benchmarks"))
from generate import generate_assets  # noqa: E402

# All tests in this module are benchmarks
pytestmark = pytest.mark.benchmark


# ── Fixtures ─────────────────────────────────────────────────


@pytest.fixture(scope="module")
def assets_1k() -> tuple[list[Asset], list[Dependency]]:
    return generate_assets(1_000)


@pytest.fixture(scope="module")
def assets_5k() -> tuple[list[Asset], list[Dependency]]:
    return generate_assets(5_000)


@pytest.fixture(scope="module")
def assets_10k() -> tuple[list[Asset], list[Dependency]]:
    return generate_assets(10_000)


@pytest.fixture(scope="module")
def graph_10k(assets_10k: tuple[list[Asset], list[Dependency]]) -> AssetGraph:
    assets, deps = assets_10k
    asset_dict = {a.id: a for a in assets}
    return AssetGraph.build(asset_dict, deps)


@pytest.fixture(scope="module")
def registry_10k(assets_10k: tuple[list[Asset], list[Dependency]]) -> Registry:
    assets, _ = assets_10k
    registry = Registry()
    registry.register_many(assets)
    return registry


# ── Asset.fingerprint ────────────────────────────────────────


def test_bench_fingerprint_cached(benchmark: pytest.fixture) -> None:
    """Fingerprint access after cache is populated (~0 cost)."""
    asset = Asset(
        id="test",
        type="model",
        description="x",
        tags=["a", "b"],
        metadata={"k": "v"},
    )
    _ = asset.fingerprint  # populate cache

    benchmark(lambda: asset.fingerprint)


def test_bench_fingerprint_uncached_1k(
    benchmark: pytest.fixture,
    assets_1k: tuple[list[Asset], list[Dependency]],
) -> None:
    """Fingerprint computation for 1K fresh assets."""
    assets, _ = assets_1k

    def compute_all() -> None:
        for a in assets:
            a._fingerprint_cache = None
            _ = a.fingerprint

    benchmark(compute_all)


# ── Registry.register_many ───────────────────────────────────


def test_bench_register_many_1k(
    benchmark: pytest.fixture,
    assets_1k: tuple[list[Asset], list[Dependency]],
) -> None:
    """Batch-register 1K assets."""
    assets, _ = assets_1k
    registry = Registry()

    def register() -> None:
        registry.clear()
        registry.register_many(assets)

    benchmark(register)


def test_bench_register_many_5k(
    benchmark: pytest.fixture,
    assets_5k: tuple[list[Asset], list[Dependency]],
) -> None:
    """Batch-register 5K assets."""
    assets, _ = assets_5k
    registry = Registry()

    def register() -> None:
        registry.clear()
        registry.register_many(assets)

    benchmark(register)


def test_bench_register_many_10k(
    benchmark: pytest.fixture,
    assets_10k: tuple[list[Asset], list[Dependency]],
) -> None:
    """Batch-register 10K assets."""
    assets, _ = assets_10k
    registry = Registry()

    def register() -> None:
        registry.clear()
        registry.register_many(assets)

    benchmark(register)


# ── AssetGraph.build ─────────────────────────────────────────


def test_bench_graph_build_1k(
    benchmark: pytest.fixture,
    assets_1k: tuple[list[Asset], list[Dependency]],
) -> None:
    """Build AssetGraph with indexes for 1K assets."""
    assets, deps = assets_1k
    asset_dict = {a.id: a for a in assets}
    benchmark(lambda: AssetGraph.build(asset_dict, deps))


def test_bench_graph_build_10k(
    benchmark: pytest.fixture,
    assets_10k: tuple[list[Asset], list[Dependency]],
) -> None:
    """Build AssetGraph with indexes for 10K assets."""
    assets, deps = assets_10k
    asset_dict = {a.id: a for a in assets}
    benchmark(lambda: AssetGraph.build(asset_dict, deps))


# ── Differ.diff ──────────────────────────────────────────────


def test_bench_differ_no_changes_1k(
    benchmark: pytest.fixture,
    assets_1k: tuple[list[Asset], list[Dependency]],
) -> None:
    """Differ with zero changes (all fingerprints match) — 1K assets."""
    assets, _ = assets_1k
    differ = Differ()
    current = {
        a.id: AssetState(
            id=a.id,
            type=a.type,
            fingerprint=a.fingerprint,
            data=a.model_dump(),
        )
        for a in assets
    }
    benchmark(lambda: differ.diff(assets, current))


def test_bench_differ_no_changes_10k(
    benchmark: pytest.fixture,
    assets_10k: tuple[list[Asset], list[Dependency]],
) -> None:
    """Differ with zero changes — 10K assets."""
    assets, _ = assets_10k
    differ = Differ()
    current = {
        a.id: AssetState(
            id=a.id,
            type=a.type,
            fingerprint=a.fingerprint,
            data=a.model_dump(),
        )
        for a in assets
    }
    benchmark(lambda: differ.diff(assets, current))


def test_bench_differ_1pct_changes_10k(
    benchmark: pytest.fixture,
    assets_10k: tuple[list[Asset], list[Dependency]],
) -> None:
    """Differ with 1% changed assets (100 out of 10K)."""
    assets, _ = assets_10k
    differ = Differ()

    # Build current state — first 100 have old fingerprints
    current: dict[str, AssetState] = {}
    for i, a in enumerate(assets):
        fp = "stale_" + a.fingerprint if i < 100 else a.fingerprint
        current[a.id] = AssetState(
            id=a.id,
            type=a.type,
            fingerprint=fp,
            data=a.model_dump(),
        )

    benchmark(lambda: differ.diff(assets, current))


# ── SQLiteBackend.save ───────────────────────────────────────


def test_bench_save_full_1k(
    benchmark: pytest.fixture,
    assets_1k: tuple[list[Asset], list[Dependency]],
    tmp_path: Path,
) -> None:
    """Full save of 1K assets to SQLite."""
    assets, _ = assets_1k
    backend = SQLiteBackend(db_path=tmp_path / "bench.db")
    snapshot = StateSnapshot(
        environment="bench",
        assets={
            a.id: AssetState(
                id=a.id,
                type=a.type,
                fingerprint=a.fingerprint,
                data=a.model_dump(),
            )
            for a in assets
        },
    )
    benchmark(lambda: backend.save("bench", snapshot))
    backend.close()


def test_bench_save_incremental_10k(
    benchmark: pytest.fixture,
    assets_10k: tuple[list[Asset], list[Dependency]],
    tmp_path: Path,
) -> None:
    """Incremental save of 30 changed assets out of 10K."""
    assets, _ = assets_10k
    backend = SQLiteBackend(db_path=tmp_path / "bench.db")
    snapshot = StateSnapshot(
        environment="bench",
        assets={
            a.id: AssetState(
                id=a.id,
                type=a.type,
                fingerprint=a.fingerprint,
                data=a.model_dump(),
            )
            for a in assets
        },
    )
    # Initial full save
    backend.save("bench", snapshot)

    # Incremental: only 30 IDs
    changed_ids = {f"model_{i:05d}" for i in range(30)}
    benchmark(lambda: backend.save("bench", snapshot, changed_ids=changed_ids))
    backend.close()


# ── Selector (indexed) ───────────────────────────────────────


def test_bench_selector_tag_10k(
    benchmark: pytest.fixture,
    registry_10k: Registry,
) -> None:
    """Select by tag on a 10K-asset graph (indexed O(1) lookup)."""
    selector = GraphSelector(registry_10k)
    benchmark(lambda: selector.execute("tag:pii"))


# ── Topological sort (cached) ────────────────────────────────


def test_bench_topo_sort_cached_10k(
    benchmark: pytest.fixture,
    graph_10k: AssetGraph,
) -> None:
    """Topological sort on 10K graph (cached after first call)."""
    _ = graph_10k.topological_sort()  # populate cache
    benchmark(lambda: graph_10k.topological_sort())
