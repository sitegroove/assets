"""CompiledCache — per-file persistent cache for asset definitions."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from pydantic import BaseModel


class CompiledEntry(BaseModel):
    """A single cached asset compilation result."""

    source_path: str
    source_mtime: int  # nanosecond mtime
    content_hash: str  # sha256 of source file content
    data: dict[str, Any]  # merged dict, ready for Pydantic


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

    def _cache_path(self, source_path: Path, root: Path) -> Path:
        """Compute the cache file path for a given source file."""
        try:
            rel = source_path.resolve().relative_to(root.resolve())
        except ValueError:
            rel = Path(source_path.name)
        return self._cache_dir / rel.with_suffix(".json")

    @staticmethod
    def _content_hash(file_path: Path) -> str:
        return hashlib.sha256(file_path.read_bytes()).hexdigest()

    @staticmethod
    def _mtime_ns(file_path: Path) -> int:
        return file_path.stat().st_mtime_ns

    def get(self, source_path: Path, root: Path) -> dict[str, Any] | None:
        """Return cached asset dict if fresh, None if stale or missing."""
        cp = self._cache_path(source_path, root)
        if not cp.exists():
            return None

        try:
            entry = CompiledEntry.model_validate_json(cp.read_text())
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
            return entry.data

        # Slow path: content hash (handles git checkout, CI clone)
        current_hash = self._content_hash(source_path)
        if entry.content_hash == current_hash:
            # Update mtime in cache for next fast-path hit
            entry.source_mtime = current_mtime
            self._write_entry(cp, entry)
            return entry.data

        # Real change
        return None

    def put(self, source_path: Path, root: Path, data: dict[str, Any]) -> None:
        """Store compiled asset dict."""
        cp = self._cache_path(source_path, root)
        entry = CompiledEntry(
            source_path=str(source_path),
            source_mtime=self._mtime_ns(source_path),
            content_hash=self._content_hash(source_path),
            data=data,
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
        path.write_text(entry.model_dump_json(indent=2))
