"""Benchmark — 500-asset comparison across state backends and file index.

Run:
    python benchmarks/bench_500_assets.py

Compares:
    State backends:   SQLiteBackend(:memory:) · SQLiteBackend(file)
    File index:       FileIndex (SQLite-backed, in state.db)
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
    FileIndex,
    SQLiteBackend,
)
from assets.state.db import connect_state
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


def _build_snapshot(
    num_assets: int = NUM_ASSETS,
    fingerprint_salt: str = "",
) -> StateSnapshot:
    assets = {
        f"model_{i:04d}": _make_asset(i, fingerprint_salt=fingerprint_salt)
        for i in range(num_assets)
    }
    deps = _make_deps(NUM_DEPS, num_assets)
    return StateSnapshot(
        environment=ENVIRONMENT,
        assets=assets,
        dependencies=deps,
        metadata={"benchmark": True, "num_assets": num_assets},
    )


def _timed(fn, iterations: int = ITERATIONS) -> dict[str, float]:
    """Run fn() multiple times and return timing stats in ms."""
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
        "stdev_ms": (statistics.stdev(times) if len(times) > 1 else 0.0),
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
    assert len(loaded.assets) == NUM_ASSETS, (
        f"Expected {NUM_ASSETS}, got {len(loaded.assets)}"
    )
    assert len(loaded.dependencies) == NUM_DEPS

    # --- List environments ---
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
    return SQLiteBackend.memory()


def _make_sqlite(tmp_dir):
    return SQLiteBackend(db_path=tmp_dir / "state.db")


# ── FileIndex Benchmarks ────────────────────────────────────
def bench_file_index(name: str, tmp_dir: Path, src_dir: Path) -> dict:
    print(f"\n{'─' * 60}")
    print(f"  File Index: {name}")
    print(f"{'─' * 60}")

    results = {}
    conn = connect_state(tmp_dir / "state.db")
    index = FileIndex(conn, tmp_dir)
    source_files = sorted(src_dir.glob("*.json"))

    # --- put_file (cold write) ---
    def _put_all():
        for src in source_files:
            data = json.loads(src.read_text())
            index.put_file(
                src,
                src_dir,
                asset_id=data["name"],
                fingerprint=hashlib.sha256(json.dumps(data).encode()).hexdigest(),
            )

    stats = _timed(_put_all, iterations=3)
    results["put_cold"] = stats
    print(f"  put_file ({NUM_ASSETS} files, cold):       {_fmt(stats)}")

    # --- diff (all fresh — mtime fast-path) ---
    discovered = [(src, src.stat().st_mtime_ns) for src in source_files]

    def _diff_all_fresh():
        status = index.diff(discovered, src_dir)
        assert len(status.fresh) == NUM_ASSETS, (
            f"Expected {NUM_ASSETS} fresh, got {len(status.fresh)}"
        )

    stats = _timed(_diff_all_fresh)
    results["diff_all_fresh"] = stats
    print(f"  diff ({NUM_ASSETS} files, all fresh):      {_fmt(stats)}")

    # --- clean (no orphans) ---
    stats = _timed(lambda: index.clean(src_dir))
    results["clean_no_orphans"] = stats
    print(f"  clean (no orphans):                 {_fmt(stats)}")

    index.close()
    return results


# ── SQLite History Benchmark ────────────────────────────────
def bench_sqlite_history(tmp_dir: Path) -> dict:
    print(f"\n{'─' * 60}")
    print("  SQLiteBackend: History Queries")
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

    # Create source files for file-index benchmarks
    src_dir = tmp_root / "source_files"
    src_dir.mkdir()
    print(f"  Generating {NUM_ASSETS} source files...")
    for i in range(NUM_ASSETS):
        (src_dir / f"model_{i:04d}.json").write_text(
            json.dumps(
                {
                    "name": f"model_{i:04d}",
                    "kind": "data_model",
                    "index": i,
                }
            )
        )

    all_results: dict[str, dict] = {}

    # ── State backends ──
    for label, factory in [
        ("SQLite(:memory:)", _make_memory),
        ("SQLite(file)", _make_sqlite),
    ]:
        sub = tmp_root / label.lower()
        sub.mkdir()
        all_results[label] = bench_state_backend(label, factory, sub)

    # ── File index ──
    sub = tmp_root / "file_index"
    sub.mkdir()
    all_results["FileIndex"] = bench_file_index("FileIndex (SQLite)", sub, src_dir)

    # ── SQLite history queries ──
    all_results["SQLiteBackend History"] = bench_sqlite_history(tmp_root)

    # ── Summary table ──
    print(f"\n{'=' * 60}")
    print("  SUMMARY (median ms)")
    print(f"{'=' * 60}")

    # State backends comparison
    print(f"\n  {'Operation':<30} {':memory:':>10} {'file':>10}")
    print(f"  {'─' * 50}")
    for op in [
        "save_first",
        "save_overwrite",
        "load",
        "list_envs",
        "delete_env",
    ]:
        row = f"  {op:<30}"
        for backend_name in ["SQLite(:memory:)", "SQLite(file)"]:
            if op in all_results.get(backend_name, {}):
                val = all_results[backend_name][op]["median_ms"]
                row += f" {val:>9.2f}"
            else:
                row += f" {'n/a':>9}"
        print(row)

    # File index
    print(f"\n  {'Operation':<30} {'SQLite':>10}")
    print(f"  {'─' * 40}")
    for op in [
        "put_cold",
        "diff_all_fresh",
        "clean_no_orphans",
    ]:
        if op in all_results.get("FileIndex", {}):
            val = all_results["FileIndex"][op]["median_ms"]
            print(f"  {op:<30} {val:>9.2f}")

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
