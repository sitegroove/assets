"""Benchmark — 500-asset comparison across all backends and compiled caches.

Run:
    python benchmarks/bench_500_assets.py

Compares:
    State backends:   MemoryBackend · LocalJSONBackend · SQLiteBackend
    Compiled caches:  CompiledCache (file-per-entry) · SQLiteCompiledCache
"""

from __future__ import annotations

import hashlib
import json
import shutil
import statistics
import sys
import tempfile
import time
from pathlib import Path

# Ensure the project root is importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from assets import (
    CompiledCache,
    LocalJSONBackend,
    MemoryBackend,
    SQLiteBackend,
    SQLiteCompiledCache,
)
from assets.state.models import AssetState, DependencyState, StateSnapshot

# ── Configuration ────────────────────────────────────────────
NUM_ASSETS = 500
NUM_DEPS = 400  # dependency edges
ITERATIONS = 5  # repeat each benchmark for statistical confidence
ENVIRONMENT = "production"


# ── Helpers ──────────────────────────────────────────────────
def _make_asset(i: int, *, fingerprint_salt: str = "") -> AssetState:
    fp = hashlib.sha256(f"asset_{i}{fingerprint_salt}".encode()).hexdigest()
    return AssetState(
        name=f"model_{i:04d}",
        kind="data_model" if i % 3 != 0 else "source",
        fingerprint=fp,
        data={
            "name": f"model_{i:04d}",
            "sql": f"SELECT * FROM raw.table_{i}",
            "tags": ["generated", f"batch_{i // 50}"],
        },
        applied_by="benchmark",
        version=1,
    )


def _make_deps(n: int, num_assets: int) -> list[DependencyState]:
    deps = []
    for i in range(n):
        src_idx = i % num_assets
        tgt_idx = (i + 1) % num_assets
        if src_idx == tgt_idx:
            tgt_idx = (tgt_idx + 1) % num_assets
        fp = hashlib.sha256(f"dep_{src_idx}_{tgt_idx}".encode()).hexdigest()
        deps.append(
            DependencyState(
                source=f"model_{src_idx:04d}",
                target=f"model_{tgt_idx:04d}",
                type="ref",
                fingerprint=fp,
            )
        )
    return deps


def _build_snapshot(num_assets: int = NUM_ASSETS, fingerprint_salt: str = "") -> StateSnapshot:
    assets = {f"model_{i:04d}": _make_asset(i, fingerprint_salt=fingerprint_salt)
              for i in range(num_assets)}
    deps = _make_deps(NUM_DEPS, num_assets)
    return StateSnapshot(
        environment=ENVIRONMENT,
        assets=assets,
        dependencies=deps,
        metadata={"benchmark": True, "num_assets": num_assets},
    )


def _timed(fn, iterations: int = ITERATIONS) -> dict[str, float]:
    """Run fn() multiple times and return timing stats in milliseconds."""
    times = []
    for _ in range(iterations):
        start = time.perf_counter()
        fn()
        elapsed = (time.perf_counter() - start) * 1000
        times.append(elapsed)
    return {
        "min_ms": min(times),
        "max_ms": max(times),
        "mean_ms": statistics.mean(times),
        "median_ms": statistics.median(times),
        "stdev_ms": statistics.stdev(times) if len(times) > 1 else 0.0,
    }


def _fmt(stats: dict[str, float]) -> str:
    return (
        f"  mean={stats['mean_ms']:8.2f}ms  "
        f"median={stats['median_ms']:8.2f}ms  "
        f"min={stats['min_ms']:8.2f}ms  "
        f"max={stats['max_ms']:8.2f}ms  "
        f"stdev={stats['stdev_ms']:7.2f}ms"
    )


# ── State Backend Benchmarks ────────────────────────────────
def bench_state_backend(name: str, make_backend, tmp_dir: Path) -> dict:
    print(f"\n{'─' * 60}")
    print(f"  State Backend: {name}")
    print(f"{'─' * 60}")

    results = {}
    snapshot = _build_snapshot()

    # --- Save (first write) ---
    backend = make_backend(tmp_dir)
    stats = _timed(lambda: backend.save(ENVIRONMENT, snapshot))
    results["save_first"] = stats
    print(f"  Save (first write, {NUM_ASSETS} assets):  {_fmt(stats)}")

    # --- Save (overwrite) ---
    updated_snapshot = _build_snapshot(fingerprint_salt="_v2")
    stats = _timed(lambda: backend.save(ENVIRONMENT, updated_snapshot))
    results["save_overwrite"] = stats
    print(f"  Save (overwrite, {NUM_ASSETS} assets):    {_fmt(stats)}")

    # --- Load ---
    stats = _timed(lambda: backend.load(ENVIRONMENT))
    results["load"] = stats
    print(f"  Load ({NUM_ASSETS} assets):               {_fmt(stats)}")

    # --- Load (verify count) ---
    loaded = backend.load(ENVIRONMENT)
    assert loaded is not None
    assert len(loaded.assets) == NUM_ASSETS, f"Expected {NUM_ASSETS}, got {len(loaded.assets)}"
    assert len(loaded.dependencies) == NUM_DEPS

    # --- List environments ---
    # Save a few more environments so list has work to do
    for env in ["staging", "dev", "pr_123", "pr_456"]:
        backend.save(env, StateSnapshot(environment=env))
    stats = _timed(lambda: backend.list_environments())
    results["list_envs"] = stats
    envs = backend.list_environments()
    print(f"  List environments ({len(envs)} envs):     {_fmt(stats)}")

    # --- Delete ---
    def _delete_and_recreate():
        backend.save("throwaway", StateSnapshot(environment="throwaway"))
        backend.delete_environment("throwaway")

    stats = _timed(_delete_and_recreate)
    results["delete_env"] = stats
    print(f"  Delete environment:                 {_fmt(stats)}")

    # Cleanup
    if hasattr(backend, "close"):
        backend.close()

    return results


def _make_memory(tmp_dir):
    return MemoryBackend()


def _make_local_json(tmp_dir):
    return LocalJSONBackend(state_dir=str(tmp_dir / "local_json"))


def _make_sqlite(tmp_dir):
    return SQLiteBackend(db_path=tmp_dir / "state.db")


# ── Compiled Cache Benchmarks ───────────────────────────────
def bench_compiled_cache(name: str, make_cache, tmp_dir: Path, src_dir: Path) -> dict:
    print(f"\n{'─' * 60}")
    print(f"  Compiled Cache: {name}")
    print(f"{'─' * 60}")

    results = {}
    cache = make_cache(tmp_dir)
    source_files = sorted(src_dir.glob("*.json"))

    # --- Put (cold write) ---
    def _put_all():
        for src in source_files:
            data = json.loads(src.read_text())
            cache.put(src, src_dir, data)

    stats = _timed(_put_all, iterations=3)
    results["put_cold"] = stats
    print(f"  Put ({NUM_ASSETS} files, cold):            {_fmt(stats)}")

    # --- Put batch (only for SQLiteCompiledCache) ---
    if hasattr(cache, "put_many"):
        # Fresh cache for fair comparison
        cache_batch = make_cache(tmp_dir / "batch")

        def _put_batch():
            entries = [
                (src, src_dir, json.loads(src.read_text())) for src in source_files
            ]
            cache_batch.put_many(entries)

        stats = _timed(_put_batch, iterations=3)
        results["put_batch"] = stats
        print(f"  Put batch ({NUM_ASSETS} files):             {_fmt(stats)}")
        if hasattr(cache_batch, "close"):
            cache_batch.close()

    # --- Get (mtime fast-path hit) ---
    def _get_all_hit():
        hits = 0
        for src in source_files:
            if cache.get(src, src_dir) is not None:
                hits += 1
        assert hits == NUM_ASSETS, f"Expected {NUM_ASSETS} hits, got {hits}"

    stats = _timed(_get_all_hit)
    results["get_hit"] = stats
    print(f"  Get ({NUM_ASSETS} files, cache hit):       {_fmt(stats)}")

    # --- Get (all miss — source deleted) ---
    missing_file = src_dir / "nonexistent.json"

    def _get_miss():
        cache.get(missing_file, src_dir)

    stats = _timed(_get_miss, iterations=ITERATIONS)
    results["get_miss"] = stats
    print(f"  Get (single miss):                  {_fmt(stats)}")

    # --- Clean (no orphans) ---
    stats = _timed(lambda: cache.clean(src_dir))
    results["clean_no_orphans"] = stats
    print(f"  Clean (no orphans):                 {_fmt(stats)}")

    if hasattr(cache, "close"):
        cache.close()

    return results


def _make_file_cache(tmp_dir):
    return CompiledCache(cache_dir=str(tmp_dir / "compiled_files"))


def _make_sqlite_cache(tmp_dir):
    return SQLiteCompiledCache(db_path=tmp_dir / "compiled.db")


# ── SQLite History Benchmark ────────────────────────────────
def bench_sqlite_history(tmp_dir: Path) -> dict:
    print(f"\n{'─' * 60}")
    print(f"  SQLiteBackend: History Queries")
    print(f"{'─' * 60}")

    results = {}
    backend = SQLiteBackend(db_path=tmp_dir / "history_bench.db")

    # Build up history: save 10 versions of all 500 assets
    print(f"  Building history: 10 versions x {NUM_ASSETS} assets...")
    for v in range(1, 11):
        snapshot = _build_snapshot(fingerprint_salt=f"_v{v}")
        for name, asset in snapshot.assets.items():
            asset.version = v
        backend.save(ENVIRONMENT, snapshot)
    print("  History built.")

    # --- asset_history (single asset, expect 10 entries) ---
    stats = _timed(lambda: backend.asset_history(ENVIRONMENT, "model_0250"))
    results["asset_history"] = stats
    history = backend.asset_history(ENVIRONMENT, "model_0250")
    print(f"  asset_history (1 asset, {len(history)} versions):  {_fmt(stats)}")

    # --- environment_changelog ---
    stats = _timed(lambda: backend.environment_changelog(ENVIRONMENT, limit=100))
    results["changelog"] = stats
    print(f"  environment_changelog (limit=100):  {_fmt(stats)}")

    # --- asset_version (point lookup) ---
    stats = _timed(lambda: backend.asset_version(ENVIRONMENT, "model_0250", 5))
    results["asset_version"] = stats
    print(f"  asset_version (point lookup):       {_fmt(stats)}")

    backend.close()
    return results


# ── Main ────────────────────────────────────────────────────
def main() -> None:
    print("=" * 60)
    print(f"  BENCHMARK: {NUM_ASSETS} assets, {NUM_DEPS} deps, {ITERATIONS} iterations")
    print("=" * 60)

    tmp_root = Path(tempfile.mkdtemp(prefix="assets_bench_"))
    print(f"  Temp dir: {tmp_root}")

    # Create source files for compiled-cache benchmarks
    src_dir = tmp_root / "source_files"
    src_dir.mkdir()
    print(f"  Generating {NUM_ASSETS} source files...")
    for i in range(NUM_ASSETS):
        (src_dir / f"model_{i:04d}.json").write_text(
            json.dumps({"name": f"model_{i:04d}", "kind": "data_model", "index": i})
        )

    all_results = {}

    # ── State backends ──
    for label, factory in [
        ("MemoryBackend", _make_memory),
        ("LocalJSONBackend", _make_local_json),
        ("SQLiteBackend", _make_sqlite),
    ]:
        sub = tmp_root / label.lower()
        sub.mkdir()
        all_results[label] = bench_state_backend(label, factory, sub)

    # ── Compiled caches ──
    for label, factory in [
        ("CompiledCache (file)", _make_file_cache),
        ("SQLiteCompiledCache", _make_sqlite_cache),
    ]:
        sub = tmp_root / label.lower().replace(" ", "_").replace("(", "").replace(")", "")
        sub.mkdir()
        all_results[label] = bench_compiled_cache(label, factory, sub, src_dir)

    # ── SQLite history queries ──
    all_results["SQLiteBackend History"] = bench_sqlite_history(tmp_root)

    # ── Summary table ──
    print(f"\n{'=' * 60}")
    print("  SUMMARY (median ms)")
    print(f"{'=' * 60}")

    # State backends comparison
    print(f"\n  {'Operation':<30} {'Memory':>10} {'LocalJSON':>10} {'SQLite':>10}")
    print(f"  {'─' * 60}")
    for op in ["save_first", "save_overwrite", "load", "list_envs", "delete_env"]:
        row = f"  {op:<30}"
        for backend_name in ["MemoryBackend", "LocalJSONBackend", "SQLiteBackend"]:
            if op in all_results.get(backend_name, {}):
                val = all_results[backend_name][op]["median_ms"]
                row += f" {val:>9.2f}"
            else:
                row += f" {'n/a':>9}"
        print(row)

    # Compiled cache comparison
    print(f"\n  {'Operation':<30} {'File':>10} {'SQLite':>10}")
    print(f"  {'─' * 50}")
    for op in ["put_cold", "put_batch", "get_hit", "get_miss", "clean_no_orphans"]:
        row = f"  {op:<30}"
        for cache_name in ["CompiledCache (file)", "SQLiteCompiledCache"]:
            if op in all_results.get(cache_name, {}):
                val = all_results[cache_name][op]["median_ms"]
                row += f" {val:>9.2f}"
            else:
                row += f" {'n/a':>9}"
        print(row)

    # History queries
    print(f"\n  {'History Query':<30} {'SQLite':>10}")
    print(f"  {'─' * 40}")
    for op in ["asset_history", "changelog", "asset_version"]:
        if op in all_results.get("SQLiteBackend History", {}):
            val = all_results["SQLiteBackend History"][op]["median_ms"]
            print(f"  {op:<30} {val:>9.2f}")

    print()

    # Cleanup
    shutil.rmtree(tmp_root, ignore_errors=True)
    print(f"  Cleaned up {tmp_root}")


if __name__ == "__main__":
    main()
