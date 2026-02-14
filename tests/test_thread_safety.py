"""Thread-safety tests for Registry, AssetGraph, and MtimeCache.

Verifies that concurrent operations do not corrupt internal state
or raise exceptions.  These tests exercise the RLock/Lock guards
added in PR8.
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from assets.core.asset import Asset
from assets.core.dependency import Dependency
from assets.core.graph import AssetGraph
from assets.core.registry import Registry
from assets.index.file import MtimeCache


# ── Registry concurrency ────────────────────────────────────


def _make_asset(i: int) -> Asset:
    """Create a simple asset with a unique id."""
    return Asset(id=f"asset-{i}", type="test", description=f"Asset {i}")


def _make_dependent_asset(i: int, dep_id: str) -> Asset:
    """Create an asset that depends on another."""
    return Asset(
        id=f"dep-asset-{i}",
        type="test",
        depends_on=[dep_id],
    )


class TestRegistryConcurrency:
    """Concurrent register/read operations on a shared Registry."""

    def test_concurrent_register_many(self) -> None:
        """Multiple threads calling register_many concurrently."""
        registry = Registry()
        n_threads = 8
        assets_per_thread = 100

        def register_batch(thread_id: int) -> None:
            assets = [
                Asset(
                    id=f"t{thread_id}-a{i}",
                    type="test",
                    description=f"Thread {thread_id} asset {i}",
                )
                for i in range(assets_per_thread)
            ]
            registry.register_many(assets)

        threads = [
            threading.Thread(target=register_batch, args=(t,)) for t in range(n_threads)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # All assets should be present (no lost writes)
        assert len(registry) == n_threads * assets_per_thread

    def test_concurrent_register_and_read(self) -> None:
        """One thread registers while another reads."""
        registry = Registry()
        stop = threading.Event()
        errors: list[Exception] = []

        def writer() -> None:
            for i in range(200):
                registry.register(_make_asset(i))

        def reader() -> None:
            try:
                while not stop.is_set():
                    _ = registry.all()
                    _ = len(registry)
                    # Access graph — may trigger rebuild
                    try:
                        _ = registry.graph
                    except Exception:
                        pass  # graph may be empty, that's ok
            except Exception as exc:
                errors.append(exc)

        reader_thread = threading.Thread(target=reader)
        writer_thread = threading.Thread(target=writer)
        reader_thread.start()
        writer_thread.start()
        writer_thread.join()
        stop.set()
        reader_thread.join()

        assert not errors, f"Reader raised: {errors}"
        assert len(registry) == 200

    def test_concurrent_register_and_unregister(self) -> None:
        """Register and unregister running concurrently."""
        registry = Registry()
        # Pre-populate
        for i in range(100):
            registry.register(_make_asset(i))

        def unregister_batch() -> None:
            for i in range(0, 50):
                registry.unregister(f"asset-{i}")

        def register_batch() -> None:
            registry.register_many(
                [Asset(id=f"new-{i}", type="test") for i in range(50)]
            )

        t1 = threading.Thread(target=unregister_batch)
        t2 = threading.Thread(target=register_batch)
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        # Should have the 50 remaining originals + 50 new = 100
        # (but some originals may have been unregistered before
        #  the new ones were added, so final count is 100)
        assert len(registry) == 100

    def test_concurrent_select(self) -> None:
        """Multiple threads selecting from the graph concurrently."""
        registry = Registry()
        assets = [
            Asset(id=f"a-{i}", type="model", tags=["important"]) for i in range(50)
        ]
        registry.register_many(assets)

        results: list[int] = []
        lock = threading.Lock()

        def do_select() -> None:
            r = registry.select("tag:important")
            with lock:
                results.append(len(r.names))

        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = [pool.submit(do_select) for _ in range(20)]
            for f in as_completed(futures):
                f.result()  # re-raise any exceptions

        assert all(r == 50 for r in results)


# ── AssetGraph concurrency ──────────────────────────────────


class TestAssetGraphConcurrency:
    """Concurrent reads on a shared AssetGraph."""

    def _build_graph(self, n: int = 100) -> AssetGraph:
        assets: dict[str, Asset] = {}
        deps: list[Dependency] = []
        for i in range(n):
            assets[f"a-{i}"] = Asset(
                id=f"a-{i}",
                type="model",
                tags=["tier1"] if i % 2 == 0 else ["tier2"],
            )
            if i > 0:
                deps.append(Dependency(source=f"a-{i - 1}", target=f"a-{i}"))
        return AssetGraph.build(assets, deps)

    def test_concurrent_topo_sort(self) -> None:
        """Multiple threads calling topological_sort concurrently."""
        graph = self._build_graph(200)
        results: list[list[str]] = []
        lock = threading.Lock()

        def do_sort() -> None:
            r = graph.topological_sort()
            with lock:
                results.append(r)

        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = [pool.submit(do_sort) for _ in range(20)]
            for f in as_completed(futures):
                f.result()

        # All results should be identical
        assert all(r == results[0] for r in results)

    def test_concurrent_fingerprint(self) -> None:
        """Multiple threads accessing graph.fingerprint concurrently."""
        graph = self._build_graph(100)
        fingerprints: list[str] = []
        lock = threading.Lock()

        def get_fp() -> None:
            fp = graph.fingerprint
            with lock:
                fingerprints.append(fp)

        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = [pool.submit(get_fp) for _ in range(20)]
            for f in as_completed(futures):
                f.result()

        assert all(fp == fingerprints[0] for fp in fingerprints)

    def test_concurrent_traversal(self) -> None:
        """Multiple threads doing ancestors/descendants concurrently."""
        graph = self._build_graph(50)
        errors: list[Exception] = []

        def traverse(name: str) -> None:
            try:
                graph.ancestors(name)
                graph.descendants(name)
                graph.stale({name})
            except Exception as exc:
                errors.append(exc)

        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = [pool.submit(traverse, f"a-{i}") for i in range(50)]
            for f in as_completed(futures):
                f.result()

        assert not errors


# ── MtimeCache concurrency ──────────────────────────────────


class TestMtimeCacheConcurrency:
    """Concurrent reads/writes on a shared MtimeCache."""

    def test_concurrent_set_and_get(self) -> None:
        """Multiple threads setting and getting keys concurrently."""
        cache = MtimeCache()  # in-memory only
        n_threads = 8
        n_ops = 200

        errors: list[Exception] = []

        def worker(thread_id: int) -> None:
            try:
                for i in range(n_ops):
                    key = f"t{thread_id}:file-{i}"
                    cache.set(key, i * 1000)
                    val = cache.get(key)
                    assert val == i * 1000, f"Expected {i * 1000}, got {val}"
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(t,)) for t in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors

    def test_concurrent_flush(self, tmp_path: Path) -> None:
        """Multiple threads flushing the cache concurrently."""
        cache_path = tmp_path / "mtime_cache.json"
        cache = MtimeCache(cache_path)

        # Pre-populate
        for i in range(100):
            cache.set(f"key-{i}", i * 1000)

        errors: list[Exception] = []

        def flush_worker() -> None:
            try:
                for _ in range(10):
                    cache.flush()
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=flush_worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors
        assert cache_path.exists()


# ── Asset fingerprint concurrency ────────────────────────────


class TestAssetFingerprintConcurrency:
    """Concurrent fingerprint access on shared Asset instances."""

    def test_concurrent_fingerprint_access(self) -> None:
        """Multiple threads accessing the same asset's fingerprint."""
        asset = Asset(
            id="test",
            type="model",
            description="A test asset",
            tags=["a", "b"],
            metadata={"key": "value"},
        )

        fingerprints: list[str] = []
        lock = threading.Lock()

        def get_fp() -> None:
            fp = asset.fingerprint
            with lock:
                fingerprints.append(fp)

        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = [pool.submit(get_fp) for _ in range(20)]
            for f in as_completed(futures):
                f.result()

        # All fingerprints should be identical
        assert len(set(fingerprints)) == 1
