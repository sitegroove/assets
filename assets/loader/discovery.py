"""FileDiscovery — discovers source files across directory groups.

Uses ``os.scandir()`` internally for performance (~6 ms for 10 000 files).

The optional :meth:`FileDiscovery.load` method orchestrates the full
discover → diff → state-rehydrate → parse → register → index cycle.
Consumers supply a :class:`Loader` implementation that owns the parsing
logic (Python module import, YAML + Jinja compilation, etc.).
"""

from __future__ import annotations

import fnmatch
import os
import re
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from assets.core.asset import Asset
    from assets.engine.manager import StateManager


# ── Loader protocol & result types ──────────────────────────


@dataclass
class LoadedAsset:
    """Result of parsing a single source file.

    Returned by :class:`Loader` implementations.

    Args:
        asset: The parsed asset instance.
        deps: File dependencies as ``(path, label)`` tuples.
            These are tracked by the index so that a change to
            any dependency marks the entry as stale.
    """

    asset: Asset
    deps: list[tuple[Path, str]] = field(default_factory=list)


@runtime_checkable
class Loader(Protocol):
    """Protocol for consumer-implemented file parsers.

    Implementations handle format-specific logic such as Python
    module imports, YAML parsing, Jinja compilation, etc.  The
    library never prescribes *how* files become assets — only the
    contract between discovery and registration.

    Example::

        class YamlSqlLoader:
            def load(self, path: Path, root: Path) -> list[LoadedAsset]:
                data = yaml.safe_load(path.read_text())
                sql_path = path.with_suffix(".sql")
                deps = [(sql_path, "sql")] if sql_path.exists() else []
                data["sql"] = sql_path.read_text() if sql_path.exists() else None
                asset = DataModel.model_validate(data)
                return [LoadedAsset(asset=asset, deps=deps)]
    """

    def load(self, path: Path, root: Path) -> list[LoadedAsset]:
        """Parse a discovered file and return assets with dependencies.

        Args:
            path: The discovered file path.
            root: The root directory of the source group
                (``SourceGroup.root``).

        Returns:
            One or more loaded assets extracted from the file.
        """
        ...


# ── Load result types ────────────────────────────────────────


@dataclass
class GroupLoadResult:
    """Per-group loading statistics."""

    group: str
    loaded: int = 0
    from_state: int = 0
    parsed: int = 0
    deleted: int = 0


@dataclass
class LoadResult:
    """Aggregate result of :meth:`FileDiscovery.load`.

    Contains per-group statistics and a convenience
    :meth:`summary` method for human-readable output.
    """

    groups: list[GroupLoadResult] = field(default_factory=list)

    @property
    def loaded(self) -> int:
        """Total assets loaded across all groups."""
        return sum(g.loaded for g in self.groups)

    @property
    def from_state(self) -> int:
        """Total assets rehydrated from state."""
        return sum(g.from_state for g in self.groups)

    @property
    def parsed(self) -> int:
        """Total assets parsed from source files."""
        return sum(g.parsed for g in self.groups)

    @property
    def deleted(self) -> int:
        """Total assets removed (source files deleted)."""
        return sum(g.deleted for g in self.groups)

    def summary(self) -> str:
        """Human-readable summary of load results."""
        lines: list[str] = []
        for g in self.groups:
            lines.append(
                f"  {g.group}: loaded={g.loaded} parsed={g.parsed} "
                f"from_state={g.from_state} deleted={g.deleted}"
            )
        totals = (
            f"  total: loaded={self.loaded} parsed={self.parsed} "
            f"from_state={self.from_state} deleted={self.deleted}"
        )
        lines.append(totals)
        return "\n".join(lines)


# ── Discovery types ──────────────────────────────────────────


@dataclass
class SourceGroup:
    """A directory of source files to scan.

    Args:
        directory: Root directory to scan (recursively).
        patterns: Glob-style patterns to match filenames
            (default ``["*.yaml"]``).  Simple extensions use fast
            ``str.endswith``; patterns with ``*`` or ``?`` elsewhere
            fall back to :func:`fnmatch.fnmatch`.
        exclude: Patterns for filenames to skip.
        name_regex: Optional compiled regex applied to filenames
            after pattern/exclude filtering.
        name: Group identifier for index isolation. Required when
            using :meth:`FileDiscovery.load`.
        root: Importable root directory / base path. Passed to
            :meth:`Loader.load` and used as the index root.
            Defaults to *directory* if not set.
        loader: Consumer-provided :class:`Loader` implementation
            that parses discovered files into assets. Required
            when using :meth:`FileDiscovery.load`.
        clean_modules: If ``True`` (the default), automatically
            purge ``sys.modules`` entries under *root* after
            loading this group. Prevents cross-contamination when
            multiple roots contain identically-named packages
            (e.g. both have ``resources/``). Set to ``False`` if
            you intentionally share modules across groups.
        setup: Optional callback invoked before loading this group.
            Use for environment preparation.
        teardown: Optional callback invoked after loading this group.
            Use for additional cleanup beyond module purging.
    """

    directory: Path
    patterns: list[str] = field(default_factory=lambda: ["*.yaml"])
    exclude: list[str] = field(default_factory=list)
    name_regex: re.Pattern[str] | None = None
    name: str = ""
    root: Path | None = None
    loader: Loader | None = None
    clean_modules: bool = True
    setup: Callable[[], None] | None = None
    teardown: Callable[[], None] | None = None


@dataclass
class DiscoveredFile:
    """A source file with pre-collected mtime."""

    path: Path
    mtime_ns: int


@dataclass
class DiscoveryResult:
    """Result of scanning source directories."""

    files: list[DiscoveredFile] = field(default_factory=list)


# ── module cache helpers ─────────────────────────────────────


def _clear_root_modules(root: Path) -> None:
    """Remove cached modules that belong to *root* from ``sys.modules``.

    When loading from multiple roots that contain identically-named
    packages (e.g. both have ``resources/``), Python's module cache
    causes cross-contamination.  Called automatically after each
    group to ensure clean import namespaces.

    This is a no-op for roots that didn't import any modules (e.g.
    YAML-only loaders).
    """
    root_str = str(root.resolve())
    to_remove = [
        name
        for name, mod in sys.modules.items()
        if mod is not None
        and hasattr(mod, "__file__")
        and mod.__file__ is not None
        and mod.__file__.startswith(root_str)
    ]
    for name in to_remove:
        del sys.modules[name]


# ── FileDiscovery ────────────────────────────────────────────


class FileDiscovery:
    """Discovers source files across multiple directory groups.

    Uses ``os.scandir()`` with manual recursion for performance.
    Returns :class:`DiscoveredFile` objects with pre-collected
    ``mtime_ns`` so callers avoid redundant ``stat()`` calls.

    The optional :meth:`load` method adds full orchestration:
    discover → diff → state-rehydrate → parse → register → index.
    """

    def __init__(self, groups: list[SourceGroup]) -> None:
        self._groups = groups

    def discover(self) -> DiscoveryResult:
        """Scan all groups and return discovered files with mtimes.

        Returns a :class:`DiscoveryResult` with files sorted by path
        and deduplicated across groups.
        """
        seen: set[str] = set()
        files: list[DiscoveredFile] = []

        for group in self._groups:
            if not group.directory.is_dir():
                continue
            matchers = _build_matchers(group.patterns)
            excluders = _build_matchers(group.exclude)
            self._scan_dir(
                group.directory,
                matchers,
                excluders,
                group.name_regex,
                seen,
                files,
            )

        files.sort(key=lambda f: f.path)
        return DiscoveryResult(files=files)

    def load(
        self,
        manager: StateManager,
        *,
        environment: str,
    ) -> LoadResult:
        """Discover, diff, rehydrate, parse, register, and index.

        For each :class:`SourceGroup` with a :attr:`~SourceGroup.loader`,
        this method performs the complete loading pipeline:

        1. **Discover** files matching the group patterns.
        2. **Diff** against the file index to classify files as
           fresh, stale (changed + new), or deleted.
        3. **Rehydrate** fresh assets from the state backend
           (no re-parsing needed).
        4. **Parse** stale files via the group's :class:`Loader`.
        5. **Register** parsed assets in the manager's registry.
        6. **Update** the file index with new fingerprints and deps.
        7. **Remove** deleted assets from registry and index.

        Args:
            manager: The :class:`StateManager` that owns the
                registry, index, and state backend.
            environment: Environment to load state from (e.g.
                ``"production"``).

        Returns:
            :class:`LoadResult` with per-group statistics.

        Raises:
            ValueError: If a group has no ``loader`` or no ``name``.
        """
        from assets.core.asset import Asset

        result = LoadResult()

        for group in self._groups:
            if group.loader is None:
                raise ValueError(
                    f"SourceGroup for directory {group.directory!r} "
                    f"has no loader. Set a Loader implementation on "
                    f"the group to use FileDiscovery.load()."
                )
            if not group.name:
                raise ValueError(
                    f"SourceGroup for directory {group.directory!r} "
                    f"has no name. Set a group name to use "
                    f"FileDiscovery.load()."
                )

            root = group.root if group.root is not None else group.directory
            group_result = self._load_group(
                manager=manager,
                group=group,
                root=root,
                environment=environment,
                asset_class=Asset,
            )
            result.groups.append(group_result)

        return result

    def _load_group(
        self,
        manager: StateManager,
        group: SourceGroup,
        root: Path,
        environment: str,
        asset_class: type[Asset],
    ) -> GroupLoadResult:
        """Load a single source group through the full pipeline."""
        assert group.loader is not None
        assert group.name

        if group.setup is not None:
            group.setup()

        registry = manager.registry
        index = manager.index
        backend = manager.backend

        # 1. Discover files for this group only
        single_discovery = FileDiscovery(groups=[group])
        discovered = single_discovery.discover()

        # 2. Diff against index
        status = index.diff(
            [(f.path, f.mtime_ns) for f in discovered.files],
            root,
            group=group.name,
        )

        counts = GroupLoadResult(group=group.name)
        batch: list[Asset] = []

        # 3. Rehydrate fresh assets from state
        snapshot = backend.load(environment)
        if snapshot is not None:
            for _path, asset_id in status.fresh:
                asset_state = snapshot.assets.get(asset_id)
                if asset_state is None:
                    continue
                batch.append(asset_class.model_validate(asset_state.data))
                counts.loaded += 1
                counts.from_state += 1

        # 4. Parse stale files via loader
        for path in status.stale:
            loaded_assets = group.loader.load(path, root)
            for loaded in loaded_assets:
                batch.append(loaded.asset)
                index.put_file(
                    path,
                    root,
                    group=group.name,
                    asset_id=loaded.asset.id,
                    fingerprint=loaded.asset.fingerprint,
                    deps=loaded.deps,
                    flush=False,
                )
                counts.loaded += 1
                counts.parsed += 1

        # Flush mtime cache once after all stale files are indexed
        if status.stale:
            index.flush_cache()

        # 5. Batch-register all assets (one graph invalidation)
        if batch:
            registry.register_many(batch)

        # 6. Handle deletions
        if status.deleted:
            registry.unregister_many(
                [name for _, name in status.deleted],
            )
            for location, _name in status.deleted:
                index.remove(location, group=group.name)
            counts.deleted = len(status.deleted)

        # 7. Clean up: purge cached modules from this root so
        # subsequent groups with identically-named packages
        # (e.g. both roots have a ``resources/`` dir) get a
        # clean import namespace.
        if group.clean_modules:
            _clear_root_modules(root)

        if group.teardown is not None:
            group.teardown()

        return counts

    def _scan_dir(
        self,
        directory: Path,
        matchers: list[_Matcher],
        excluders: list[_Matcher],
        name_regex: re.Pattern[str] | None,
        seen: set[str],
        out: list[DiscoveredFile],
    ) -> None:
        """Recursively scan a directory using ``os.scandir``."""
        try:
            entries = os.scandir(directory)
        except OSError:
            return

        with entries:
            for entry in entries:
                if entry.is_dir(follow_symlinks=False):
                    self._scan_dir(
                        Path(entry.path),
                        matchers,
                        excluders,
                        name_regex,
                        seen,
                        out,
                    )
                    continue

                if not entry.is_file(follow_symlinks=False):
                    continue

                name = entry.name

                if not _any_match(matchers, name):
                    continue
                if _any_match(excluders, name):
                    continue
                if name_regex is not None and not name_regex.search(name):
                    continue

                resolved = str(Path(entry.path).resolve())
                if resolved in seen:
                    continue
                seen.add(resolved)

                try:
                    stat = entry.stat(follow_symlinks=False)
                except OSError:
                    continue

                out.append(
                    DiscoveredFile(
                        path=Path(entry.path),
                        mtime_ns=stat.st_mtime_ns,
                    )
                )


# ── pattern matching helpers ─────────────────────────────────


class _Matcher:
    """Wraps a glob pattern: uses ``str.endswith`` for simple
    extension patterns (``*.yaml``), ``fnmatch`` otherwise.
    """

    __slots__ = ("_suffix", "_pattern")

    def __init__(self, pattern: str) -> None:
        # Simple extension pattern like "*.yaml"
        if pattern.startswith("*.") and "*" not in pattern[1:] and "?" not in pattern:
            self._suffix: str | None = pattern[1:]  # ".yaml"
            self._pattern: str | None = None
        else:
            self._suffix = None
            self._pattern = pattern

    def matches(self, name: str) -> bool:
        if self._suffix is not None:
            return name.endswith(self._suffix)
        assert self._pattern is not None
        return fnmatch.fnmatch(name, self._pattern)


def _build_matchers(patterns: list[str]) -> list[_Matcher]:
    return [_Matcher(p) for p in patterns]


def _any_match(matchers: list[_Matcher], name: str) -> bool:
    return any(m.matches(name) for m in matchers)
