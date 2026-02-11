"""CompiledCache — per-file persistent cache for asset definitions."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel


class CompiledEntry(BaseModel):
    """A single cached asset compilation result."""

    source_path: str
    source_mtime: int  # nanosecond mtime
    content_hash: str  # sha256 of source file content
    data: dict[str, Any]  # merged dict, ready for Pydantic

    # v2 fields — pre-computed values to skip recomputation on warm load.
    # Old (v1) cache files lack these; Pydantic defaults them to 1/None.
    version: int = 1
    fingerprint: str | None = None
    refs: list[str] | None = None
    depends_on: list[str] | None = None


class CompiledCache:
    """Per-file persistent cache, like __pycache__ for asset definitions.

    On-disk structure:
        .assets_state/compiled/
        ├── raw/
        │   └── users.json
        └── staging/
            └── users.json
    """

    def __init__(self, cache_dir: str = ".assets_state/compiled") -> None:
        self._cache_dir = Path(cache_dir)
        self._resolved_root_cache: dict[str, Path] = {}

    def _cache_path(self, source_path: Path, root: Path) -> Path:
        """Compute the cache file path for a given source file.

        Caches the resolved root to avoid repeated resolve() syscalls
        when loading many files under the same root.
        """
        root_key = str(root)
        resolved_root = self._resolved_root_cache.get(root_key)
        if resolved_root is None:
            resolved_root = root.resolve()
            self._resolved_root_cache[root_key] = resolved_root

        try:
            # Use relative_to with unresolved path first (avoids resolve()
            # on source_path when the path is already under root).
            rel = source_path.relative_to(root)
        except ValueError:
            try:
                rel = source_path.resolve().relative_to(resolved_root)
            except ValueError:
                rel = Path(source_path.name)
        return self._cache_dir / rel.with_suffix(".json")

    @staticmethod
    def _content_hash(file_path: Path) -> str:
        return hashlib.sha256(file_path.read_bytes()).hexdigest()

    @staticmethod
    def _mtime_ns(file_path: Path) -> int:
        return file_path.stat().st_mtime_ns

    def get(self, source_path: Path, root: Path) -> CompiledEntry | None:
        """Return cached entry if fresh, None if stale or missing.

        Returns the full CompiledEntry so callers can access pre-computed
        v2 fields (fingerprint, refs, depends_on) when available.
        """
        cp = self._cache_path(source_path, root)

        try:
            # Read + parse in one try block — avoids separate exists() stat.
            raw = json.loads(cp.read_bytes())
            entry = CompiledEntry.model_construct(**raw)
        except FileNotFoundError:
            return None
        except Exception:
            # Corrupted cache file — treat as miss
            cp.unlink(missing_ok=True)
            return None

        # Fast path: mtime match
        try:
            current_mtime = self._mtime_ns(source_path)
        except OSError:
            return None

        if entry.source_mtime == current_mtime:
            return entry

        # Slow path: content hash (handles git checkout, CI clone)
        current_hash = self._content_hash(source_path)
        if entry.content_hash == current_hash:
            # Update mtime in cache for next fast-path hit
            entry.source_mtime = current_mtime
            self._write_entry(cp, entry)
            return entry

        # Real change
        return None

    def put(
        self,
        source_path: Path,
        root: Path,
        data: dict[str, Any],
        fingerprint: str | None = None,
        refs: list[str] | None = None,
        depends_on: list[str] | None = None,
    ) -> None:
        """Store compiled asset dict with optional pre-computed v2 fields."""
        cp = self._cache_path(source_path, root)
        entry = CompiledEntry(
            source_path=str(source_path),
            source_mtime=self._mtime_ns(source_path),
            content_hash=self._content_hash(source_path),
            data=data,
            version=2,
            fingerprint=fingerprint,
            refs=refs,
            depends_on=depends_on,
        )
        self._write_entry(cp, entry)

    def clean(self, root: Path) -> int:
        """Remove compiled files with no matching source. Returns count removed."""
        if not self._cache_dir.exists():
            return 0
        removed = 0
        for cache_file in self._cache_dir.rglob("*.json"):
            try:
                rel = cache_file.relative_to(self._cache_dir)
            except ValueError:
                continue
            # Check all common source extensions
            found = False
            for ext in (".yaml", ".yml", ".py", ".sql", ".json"):
                candidate = root / rel.with_suffix(ext)
                if candidate.exists():
                    found = True
                    break
            if not found:
                cache_file.unlink()
                removed += 1
        return removed

    def _write_entry(self, path: Path, entry: CompiledEntry) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(entry.model_dump_json())
