"""assets — A declarative, graph-based asset registry powered by Pydantic."""

from assets.core.asset import Asset
from assets.core.dependency import Dependency, FieldMapping
from assets.core.fields import AssetField
from assets.core.graph import AssetGraph, SelectionResult
from assets.core.registry import Registry
from assets.engine.differ import Change, ChangeSet, Differ, FieldChange
from assets.engine.manager import ApplyResult, StateManager
from assets.engine.planner import Plan
from assets.loader.compiled import CompiledCache
from assets.loader.project import LoadError, LoadResult, ProjectLoader
from assets.resolver.lineage import LineageResolver
from assets.resolver.ref import RefResolver
from assets.selector.parser import SelectorParser
from assets.state.backend import StateBackend
from assets.state.environment import Environment, EnvironmentConfig
from assets.state.local import LocalJSONBackend
from assets.state.memory import MemoryBackend
from assets.state.models import AssetState, DependencyState, SourceFileRef, StateSnapshot

__all__ = [
    # Core
    "Asset",
    "AssetField",
    "AssetGraph",
    "Dependency",
    "FieldMapping",
    "Registry",
    "SelectionResult",
    # Resolver
    "LineageResolver",
    "RefResolver",
    # Selector
    "SelectorParser",
    # Loader
    "CompiledCache",
    "LoadError",
    "LoadResult",
    "ProjectLoader",
    # State
    "AssetState",
    "DependencyState",
    "Environment",
    "EnvironmentConfig",
    "LocalJSONBackend",
    "MemoryBackend",
    "SourceFileRef",
    "StateBackend",
    "StateSnapshot",
    # Engine
    "ApplyResult",
    "Change",
    "ChangeSet",
    "Differ",
    "FieldChange",
    "Plan",
    "StateManager",
]
