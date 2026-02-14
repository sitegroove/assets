#!/usr/bin/env python3
"""End-to-end benchmark: full load → plan → apply at scale.

Run:
    python benchmarks/bench_e2e.py                # default: 1K, 5K, 10K
    python benchmarks/bench_e2e.py --sizes 50000  # custom size
    python benchmarks/bench_e2e.py --json          # output JSON for CI

Measures:
    1. Cold load: register_many + graph build
    2. Plan (no changes): differ with all fingerprints matching
    3. Full apply (cold): save all assets to SQLite
    4. Plan after apply (warm): differ against saved state
    5. Incremental apply: save 30 changed assets
    6. Graph operations: topo sort, selector, stale analysis
"""

from __future__ import annotations

import argparse
import json
import shutil
import statistics
import sys
import tempfile
import time
import tracemalloc
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from generate import generate_assets

from assets.core.graph import AssetGraph
from assets.core.registry import Registry
from assets.engine.differ import Differ
from assets.state.models import AssetState, StateSnapshot
from assets.state.sqlite import SQLiteBackend


def _timed(fn, label: str = "", warmup: int = 0, repeat: int = 3) -> dict:
    """Time a function, return stats dict."""
    for _ in range(warmup):
        fn()
    times = []
    for _ in range(repeat):
        start = time.perf_counter()
        result = fn()
        elapsed = (time.perf_counter() - start) * 1000
        times.append(elapsed)
    stats = {
        "label": label,
        "min_ms": round(min(times), 2),
        "median_ms": round(statistics.median(times), 2),
        "max_ms": round(max(times), 2),
        "mean_ms": round(statistics.mean(times), 2),
    }
    return stats


def _peak_memory_mb(fn) -> tuple[float, object]:
    """Measure peak memory of fn() using tracemalloc."""
    tracemalloc.start()
    result = fn()
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    return round(peak / 1024 / 1024, 2), result


def run_benchmark(n: int, tmp_dir: Path) -> dict:
    """Run full benchmark suite for N assets."""
    print(f"\n{'=' * 70}")
    print(f"  E2E Benchmark: {n:,} assets")
    print(f"{'=' * 70}")

    results: dict[str, dict] = {}

    # 1. Generate assets
    print(f"  Generating {n:,} assets...")
    gen_start = time.perf_counter()
    assets, deps = generate_assets(n)
    gen_ms = (time.perf_counter() - gen_start) * 1000
    print(f"  Generated in {gen_ms:.0f}ms ({len(deps):,} dependencies)")
    results["generate"] = {"label": "generate", "median_ms": round(gen_ms, 2)}

    # 2. Cold load: register_many
    def do_register() -> Registry:
        r = Registry()
        r.register_many(assets)
        return r

    stats = _timed(do_register, "register_many")
    print(f"  register_many:   {stats['median_ms']:>8.1f}ms")
    results["register_many"] = stats

    # Get a populated registry for remaining tests
    registry = Registry()
    registry.register_many(assets)

    # 3. Graph build
    asset_dict = {a.id: a for a in assets}
    stats = _timed(lambda: AssetGraph.build(asset_dict, deps), "graph_build")
    print(f"  graph_build:     {stats['median_ms']:>8.1f}ms")
    results["graph_build"] = stats

    # Peak memory for graph
    mem_mb, graph = _peak_memory_mb(lambda: AssetGraph.build(asset_dict, deps))
    print(f"  graph memory:    {mem_mb:>8.1f}MB")
    results["graph_memory_mb"] = {"label": "graph_memory_mb", "value": mem_mb}

    # 4. Topological sort (cold then cached)
    fresh_graph = AssetGraph.build(asset_dict, deps)
    stats = _timed(fresh_graph.topological_sort, "topo_sort_cold", repeat=1)
    print(f"  topo_sort cold:  {stats['median_ms']:>8.1f}ms")
    results["topo_sort_cold"] = stats

    stats = _timed(fresh_graph.topological_sort, "topo_sort_cached")
    print(f"  topo_sort cache: {stats['median_ms']:>8.1f}ms")
    results["topo_sort_cached"] = stats

    # 5. Selector (indexed)
    stats = _timed(lambda: fresh_graph.select("tag:pii"), "select_tag")
    print(f"  select tag:pii:  {stats['median_ms']:>8.1f}ms")
    results["select_tag"] = stats

    # 6. Stale analysis
    changed = {f"model_{i:05d}" for i in range(30)}
    stats = _timed(lambda: fresh_graph.stale(changed), "stale_30")
    stale_result = fresh_graph.stale(changed)
    print(
        f"  stale(30 roots): {stats['median_ms']:>8.1f}ms ({len(stale_result)} impacted)"
    )
    results["stale_30"] = stats

    # 7. Differ — no changes
    differ = Differ()
    current_state = {
        a.id: AssetState(
            id=a.id,
            type=a.type,
            fingerprint=a.fingerprint,
            data=a.model_dump(),
        )
        for a in assets
    }
    stats = _timed(lambda: differ.diff(assets, current_state), "differ_no_changes")
    print(f"  differ (0 chg):  {stats['median_ms']:>8.1f}ms")
    results["differ_no_changes"] = stats

    # 8. Differ — 1% changed
    stale_state = dict(current_state)
    for i in range(n // 100):
        key = f"model_{i:05d}"
        s = stale_state[key]
        stale_state[key] = AssetState(
            id=s.id,
            type=s.type,
            fingerprint="stale_" + s.fingerprint,
            data=s.data,
        )
    stats = _timed(lambda: differ.diff(assets, stale_state), "differ_1pct")
    print(f"  differ (1% chg): {stats['median_ms']:>8.1f}ms")
    results["differ_1pct"] = stats

    # 9. SQLite full save
    db_path = tmp_dir / f"bench_{n}.db"
    backend = SQLiteBackend(db_path=db_path)
    snapshot = StateSnapshot(
        environment="bench",
        assets=current_state,
    )
    stats = _timed(lambda: backend.save("bench", snapshot), "save_full", repeat=2)
    print(f"  save_full:       {stats['median_ms']:>8.1f}ms")
    results["save_full"] = stats

    # DB file size
    db_size_mb = round(db_path.stat().st_size / 1024 / 1024, 2)
    print(f"  DB size:         {db_size_mb:>8.1f}MB")
    results["db_size_mb"] = {"label": "db_size_mb", "value": db_size_mb}

    # 10. SQLite incremental save (30 assets)
    changed_ids = {f"model_{i:05d}" for i in range(30)}
    stats = _timed(
        lambda: backend.save("bench", snapshot, changed_ids=changed_ids),
        "save_incremental_30",
    )
    print(f"  save_incr(30):   {stats['median_ms']:>8.1f}ms")
    results["save_incremental_30"] = stats

    # 11. SQLite load
    stats = _timed(lambda: backend.load("bench"), "load")
    print(f"  load:            {stats['median_ms']:>8.1f}ms")
    results["load"] = stats

    backend.close()
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description="E2E benchmark")
    parser.add_argument(
        "--sizes",
        nargs="+",
        type=int,
        default=[1_000, 5_000, 10_000],
        help="Asset counts to benchmark (default: 1000 5000 10000)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output results as JSON",
    )
    args = parser.parse_args()

    tmp_root = Path(tempfile.mkdtemp(prefix="assets_e2e_"))
    all_results: dict[str, dict] = {}

    try:
        for size in args.sizes:
            sub = tmp_root / f"n{size}"
            sub.mkdir()
            all_results[f"{size}"] = run_benchmark(size, sub)

        # Summary table
        print(f"\n{'=' * 70}")
        print("  SUMMARY (median ms)")
        print(f"{'=' * 70}")

        ops = [
            "register_many",
            "graph_build",
            "topo_sort_cold",
            "differ_no_changes",
            "differ_1pct",
            "save_full",
            "save_incremental_30",
            "load",
        ]
        header = f"  {'Operation':<25}" + "".join(f" {s:>10}" for s in args.sizes)
        print(header)
        print(f"  {'─' * (25 + 11 * len(args.sizes))}")
        for op in ops:
            print(
                f"  {op:<25}"
                + "".join(
                    f" {all_results.get(str(s), {}).get(op, {}).get('median_ms', 0):>10.1f}"
                    for s in args.sizes
                )
            )

        if args.json:
            print(f"\n{json.dumps(all_results, indent=2)}")

    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)
        print(f"\n  Cleaned up {tmp_root}")


if __name__ == "__main__":
    main()
