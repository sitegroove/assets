"""assets — A declarative, graph-based asset registry powered by Pydantic."""

from assets.core.asset import Asset
from assets.core.dependency import Dependency, FieldMapping
from assets.core.fields import AssetField
from assets.core.graph import AssetGraph, SelectionResult
from assets.core.registry import Registry
from assets.engine.differ import Change, ChangeSet, Differ, FieldChange
from assets.engine.manager import ApplyResult, StateManager
from assets.engine.planner import Plan
from assets.index.base import Index
from assets.index.file import FileIndex, IndexStatus
from assets.loader.discovery import (
    DiscoveredFile,
    DiscoveryResult,
    FileDiscovery,
    GroupLoadResult,
    LoadedAsset,
    Loader,
    LoadResult,
    SourceGroup,
)
from assets.project import Project
from assets.resolver.lineage import DependencyResolver
from assets.selector.base import Selector
from assets.selector.parser import GraphSelector, SelectorParser
from assets.state.backend import StateBackend
from assets.state.environment import Environment, EnvironmentConfig
from assets.state.models import (
    AssetState,
    DependencyState,
    StateSnapshot,
)
from assets.state.sqlite import SQLiteBackend
from assets.state.tiered import TieredBackend

__all__ = [
    # Core
    "Asset",
    "AssetField",
    "Project",
    "AssetGraph",
    "Dependency",
    "FieldMapping",
    "Registry",
    "SelectionResult",
    # Index
    "FileIndex",
    "Index",
    "IndexStatus",
    # Loader
    "DiscoveredFile",
    "DiscoveryResult",
    "FileDiscovery",
    "GroupLoadResult",
    "LoadedAsset",
    "LoadResult",
    "Loader",
    "SourceGroup",
    # Resolver
    "DependencyResolver",
    # Selector
    "GraphSelector",
    "Selector",
    "SelectorParser",
    # State
    "AssetState",
    "DependencyState",
    "Environment",
    "EnvironmentConfig",
    "SQLiteBackend",
    "StateBackend",
    "StateSnapshot",
    "TieredBackend",
    # Engine
    "ApplyResult",
    "Change",
    "ChangeSet",
    "Differ",
    "FieldChange",
    "Plan",
    "StateManager",
]
