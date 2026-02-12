# Architecture

This document describes the internal architecture of the `assets` library — its module structure, data flow, extension points, and design rationale. Use this as a reference when contributing or building on top of the library.

## High-Level Overview

```
┌──────────────────────────────────────────────────────────────────┐
│                        Consumer Code                             │
│  (defines Asset subclasses, loaders, lineage resolvers)          │
└──────────────┬───────────────────────────────────────────────────┘
               │
┌──────────────▼───────────────────────────────────────────────────┐
│                      assets (this library)                        │
│                                                                   │
│  ┌─────────┐  ┌──────────┐  ┌──────────┐  ┌──────────────────┐  │
│  │  Core   │  │ Resolver │  │  Loader  │  │     Engine       │  │
│  │         │  │          │  │          │  │                  │  │
│  │ Asset   │◄─│ Ref      │  │ Compiled │  │ Differ           │  │
│  │ Fields  │  │ Lineage  │  │ Cache    │  │ Planner          │  │
│  │ Dep     │  │ (ABC)    │  │ Project  │  │ StateManager     │  │
│  │ Graph   │  └──────────┘  │ (base)   │  │ (plan/apply/     │  │
│  │ Registry│                └──────────┘  │  drift/promote)  │  │
│  └─────────┘                              └──────────────────┘  │
│                                                                   │
│  ┌───────────┐  ┌──────────────────────────────────────────────┐ │
│  │ Selector  │  │              State                           │ │
│  │           │  │ Models | Environments | Backend (ABC)        │ │
│  │ Parser    │  │ MemoryBackend | LocalJSONBackend             │ │
│  └───────────┘  └──────────────────────────────────────────────┘ │
└──────────────────────────────────────────────────────────────────┘
```

## Module Map

### `assets/core/` — Foundation Models

Everything in the library builds on these models.

| File | Key Types | Purpose |
|---|---|---|
| `fields.py` | `AssetField()` | Pydantic `Field()` wrapper with `fingerprint` metadata |
| `asset.py` | `Asset` | Base model: name, kind, tags, sql, metadata, children. Computed `fingerprint` (SHA-256). Child introspection via `get_child()`, `list_children()`, `get_child_at()`. Recursive nesting via `children: list[Asset]` |
| `dependency.py` | `Dependency`, `FieldMapping` | Graph edge (source→target) with type and computed fingerprint. FieldMapping uses path-based `source`/`target` (e.g., `"raw.users/email"`) with `/` separator |
| `graph.py` | `AssetGraph`, `SelectionResult` | DAG built from assets + dependencies. Traversal (`ancestors`, `descendants`), `topological_sort()`, `roots()`, `leaves()` |
| `registry.py` | `Registry` | Central store. `register()` extracts SQL refs, builds dependencies. Lazy graph. Delegates selector queries and lineage resolution |

**Data flow:**
```
Asset created → Registry.register()
  → RefResolver.extract_refs(sql)    # {{ ref('x') }} → ["x"]
  → asset.depends_on = refs
  → Dependency objects appended
  → graph invalidated (rebuilt lazily)
```

### `assets/resolver/` — Reference and Lineage Resolution

| File | Key Types | Purpose |
|---|---|---|
| `ref.py` | `RefResolver` | Regex `{{ ref('name') }}` extraction. Runs on every `register()`. ~0.01ms per model |
| `lineage.py` | `LineageResolver` (ABC) | Abstract base class. Consumers implement `resolve(definition, upstream_fields) → list[FieldMapping]` with their own parser (e.g., sqlglot for SQL, custom parsers for other formats) |

**Ref resolution is automatic; lineage resolution is never automatic.**

The library extracts asset-level dependencies via regex at registration time. Field-level dependency resolution is explicitly requested by the consumer via `registry.resolve_field_dependency()`, passing their own resolver instance.

### `assets/selector/` — Query Language

| File | Key Types | Purpose |
|---|---|---|
| `parser.py` | `SelectorParser` | Parse and execute dbt-style selectors against an `AssetGraph` |

**Supported syntax:**

| Pattern | Example | Behavior |
|---|---|---|
| Exact name | `staging.users` | Single asset |
| Tag filter | `tag:pii` | All with tag |
| Kind filter | `kind:data_model` | All with kind |
| Wildcard | `raw.*` | Glob match on name |
| Upstream | `+staging.users` | Asset + all ancestors |
| Downstream | `staging.users+` | Asset + all descendants |
| Both | `+staging.users+` | Ancestors + self + descendants |
| Depth-limited | `staging.users+2` | Descendants up to depth 2 |
| Intersection | `tag:pii,kind:data_model` | AND of multiple selectors |

The parser uses regex to detect graph traversal patterns (`+name+2`) and delegates traversal to `AssetGraph.ancestors()`/`descendants()` with optional `max_depth`.

### `assets/loader/` — File Loading and Caching

| File | Key Types | Purpose |
|---|---|---|
| `compiled.py` | `CompiledCache`, `CompiledEntry` | Per-file persistent cache. Stores compiled asset dicts as JSON. Three-tier freshness check: mtime → content hash → miss |
| `project.py` | `ProjectLoader`, `LoadResult`, `LoadError` | Base loader. Discovers files, checks cache, parses, registers. Consumers subclass `parse_file()` and `discover_files()` for their formats |

**Cache check flow:**
```
1. Does compiled cache file exist?
   No  → parse source, write cache, return             (cache miss)
   Yes ↓
2. Does mtime match?
   Yes → return cached data                             (fastest: ~0.1ms)
   No  ↓
3. Does content hash match? (git checkout, CI clone)
   Yes → update mtime in cache, return                  (git checkout case)
   No  → parse source, write cache, return              (real change)
```

**On-disk layout:**
```
.assets_state/compiled/
├── models/
│   ├── users.json      ← compiled from models/users.yaml
│   └── payments.json
└── staging/
    └── users.json
```

**Extension point:** Consumers subclass `ProjectLoader`:
```python
class YAMLLoader(ProjectLoader):
    def discover_files(self, project_dir: Path) -> list[Path]: ...
    def parse_file(self, path: Path, root: Path) -> dict | None: ...
```

### `assets/state/` — Persistence and Environments

| File | Key Types | Purpose |
|---|---|---|
| `models.py` | `StateSnapshot`, `AssetState`, `DependencyState`, `SourceFileRef` | Pydantic models for persisted state |
| `environment.py` | `Environment`, `EnvironmentConfig` | Environment definition (name, parent, shallow flag) and multi-environment config |
| `backend.py` | `StateBackend` (ABC) | Abstract interface: `load()`, `save()`, `lock()`, `list_environments()`, `delete_environment()` |
| `memory.py` | `MemoryBackend` | In-memory implementation for testing. No file I/O |
| `local.py` | `LocalJSONBackend` | JSON files on disk with file-based locking. Stale lock detection (60s timeout) |

**State model hierarchy:**
```
StateSnapshot
├── version: int
├── environment: str
├── created_at / updated_at: datetime
├── assets: dict[str, AssetState]
│   └── AssetState
│       ├── name, kind, fingerprint
│       ├── data: dict              ← full serialized asset
│       ├── source_files: list[SourceFileRef]
│       ├── applied_at, applied_by
│       ├── version: int
│       └── deleted: bool           ← tombstone for shallow envs
├── dependencies: list[DependencyState]
└── metadata: dict
```

**Environment types:**

| Type | shallow | What it stores | Use case |
|---|---|---|---|
| Full | `False` | Complete state of all assets | production, staging |
| Shallow | `True` | Only assets the developer touched | dev branches, PRs |

Shallow environments inherit from their parent. When resolving state for a shallow env, the library walks the parent chain and overlays local changes on top. Deletions are stored as tombstones (`deleted=True`) so they don't fall through to the parent.

**Backend on-disk layout (LocalJSON):**
```
.assets_state/environments/
├── production/
│   ├── state.json
│   └── state.json.lock     ← file-based lock
├── staging/
│   └── state.json
└── dev_alice/
    └── state.json           ← tiny, only overrides
```

### `assets/engine/` — Plan/Apply/Drift/Promote

| File | Key Types | Purpose |
|---|---|---|
| `differ.py` | `Differ`, `Change`, `ChangeSet`, `FieldChange` | Fingerprint-first comparison. Deep field-level diff only when fingerprints differ |
| `planner.py` | `Plan` | Plan model wrapping a ChangeSet. `show()` for pretty-print. `has_changes` property |
| `manager.py` | `StateManager`, `ApplyResult`, `ResolvedState` | Orchestrator: `plan()`, `apply()`, `drift()`, `promote()`, `create_environment()`, `destroy_environment()` |

**Differ algorithm:**
```
For each desired asset:
  1. Not in current state?          → CREATE
  2. In state but deleted?          → CREATE (resurrect)
  3. Fingerprint matches?           → SKIP (fast path)
  4. Fingerprint differs?           → UPDATE + deep diff fields

For each asset in state but not desired:
  → DELETE
```

**StateManager.plan() flow:**
```
plan(project_dir, environment, selector)
  1. registry.clear()
  2. loader.load(project_dir)           ← uses compiled cache
  3. if selector: registry.select()     ← filter desired assets
  4. _resolve_state(env)                ← walk parent chain for shallow envs
  5. differ.diff(desired, current)      ← fingerprint-first
  6. return Plan(changeset)
```

**StateManager.apply() flow:**
```
apply(plan, environment)
  1. backend.lock(env)                  ← acquire distributed lock
  2. backend.load(env)                  ← get current state
  3. for each change:
     - CREATE: add AssetState
     - UPDATE: replace AssetState
     - DELETE: remove (or tombstone for shallow)
  4. backend.save(env, state)           ← persist
  5. backend.unlock()                   ← release lock
  6. return ApplyResult
```

**StateManager.promote() flow:**
```
promote(from_env, to_env, selector)
  1. Load source env state
  2. Resolve target env state (with parent chain)
  3. Build desired from source (optionally filtered)
  4. Diff desired vs. target
  5. Return Plan for target env
```

## Extension Points

The library is designed to be extended by consumers at these points:

### 1. Asset Subclasses

```python
class Column(Asset):
    type: str = ""
    pii: bool = False

class DataModel(Asset):
    row_count: int = AssetField(default=0, fingerprint=False)
```

- Add any Pydantic fields
- Use `AssetField(fingerprint=False)` to exclude from change detection
- Nest child assets via the inherited `children: list[Asset]` field
- Override `children` type for specific subtypes: `children: list[Column] = []`

### 2. File Loaders (`ProjectLoader`)

Override `discover_files()` and `parse_file()` for any format:
- YAML + companion SQL files
- Python modules with decorators
- TOML configuration
- Database introspection

### 3. Lineage Resolvers (`LineageResolver`)

Implement `resolve(definition, upstream_fields) → list[FieldMapping]`:
- sqlglot-based SQL column tracing
- Spark DataFrame lineage parser
- pandas operation chain analyzer
- YAML pipeline config resolver
- External catalog integration

### 4. State Backends (`StateBackend`)

Implement the 5 abstract methods for any storage. Built-in backends:
- `MemoryBackend` — in-memory (testing)
- `LocalJSONBackend` — local filesystem JSON files
- `SQLiteBackend` — single SQLite database file
- `FsspecBackend` — any fsspec-compatible URL (S3, GCS, Azure, etc.)

Custom backends can implement the same interface for:
- PostgreSQL
- Redis
- Git-backed state

## Fingerprint System

Fingerprints are the foundation of the change detection system.

**Asset fingerprint:**
- SHA-256 of all fields where `fingerprint != False`
- Computed on access via `@computed_field` (never stored)
- Deterministic: same inputs always produce same hash
- Excludes `row_count`, `last_synced_at`, etc. when marked `fingerprint=False`

**Dependency fingerprint:**
- SHA-256 of `source:target:type` string
- Used for dependency change detection

**Graph fingerprint:**
- SHA-256 of all asset fingerprints + topology
- Single string for "has anything changed in the entire graph?"

**Change detection layers (each filters before next):**

| Layer | What it checks | Cost | Result |
|---|---|---|---|
| Compiled cache mtime | File modification time | ~1us | "might have changed" |
| Compiled cache hash | File content SHA-256 | ~0.1us (cached) | "file content changed" |
| Asset fingerprint | Fingerprinted fields | ~0.1us | "asset definition changed" |
| Deep diff | Field-by-field comparison | ~1ms | "what exactly changed" |
| Lineage (on-demand) | Column-level SQL analysis | ~10ms | "which columns affected" |

## Data Model Relationships

```
Registry
├── _assets: dict[str, Asset]           ← name → asset
├── _dependencies: list[Dependency]     ← edges
├── _ref_resolver: RefResolver          ← regex extraction
└── _graph: AssetGraph (lazy)
    ├── _assets: dict[str, Asset]
    ├── _forward: dict[str, set[str]]   ← parent → children
    └── _backward: dict[str, set[str]]  ← child → parents

StateManager
├── registry: Registry
├── loader: ProjectLoader
│   └── cache: CompiledCache
├── backend: StateBackend
│   └── MemoryBackend | LocalJSONBackend | (custom)
└── env_config: EnvironmentConfig
    └── environments: dict[str, Environment]
```

## Key Invariants

1. **`register()` always extracts refs** — if an asset has SQL, its `depends_on` is populated automatically
2. **Graph is lazily built** — invalidated on every `register()`, rebuilt on first access
3. **Fingerprints are computed, never stored** on the Asset — they're derived from field values
4. **State stores fingerprints** — `AssetState.fingerprint` is the hash at apply time
5. **Plan never mutates state** — it only reads and compares
6. **Apply always acquires a lock** — concurrent applies to the same env are serialized
7. **Shallow envs never store unmodified assets** — only overrides and tombstones
8. **Protected environments cannot be destroyed** — `production` and `staging` are protected
9. **Lineage never runs automatically** — only when explicitly called by the consumer

## File Inventory

```
assets/
├── __init__.py              # Public API: 30 exports
├── core/
│   ├── __init__.py
│   ├── fields.py            # AssetField(), FINGERPRINT_KEY
│   ├── asset.py             # Asset (BaseModel), _serialize_value(), model_rebuild()
│   ├── dependency.py        # Dependency, FieldMapping
│   ├── graph.py             # AssetGraph, SelectionResult
│   └── registry.py          # Registry
├── loader/
│   ├── __init__.py
│   ├── compiled.py          # CompiledCache, CompiledEntry
│   └── project.py           # ProjectLoader, LoadResult, LoadError
├── resolver/
│   ├── __init__.py
│   ├── ref.py               # RefResolver
│   └── lineage.py           # LineageResolver (ABC)
├── selector/
│   ├── __init__.py
│   └── parser.py            # SelectorParser
├── state/
│   ├── __init__.py
│   ├── models.py            # StateSnapshot, AssetState, DependencyState, SourceFileRef
│   ├── environment.py       # Environment, EnvironmentConfig
│   ├── backend.py           # StateBackend (ABC)
│   ├── memory.py            # MemoryBackend
│   └── local.py             # LocalJSONBackend
└── engine/
    ├── __init__.py
    ├── differ.py            # Differ, Change, ChangeSet, FieldChange
    ├── planner.py           # Plan
    └── manager.py           # StateManager, ApplyResult, ResolvedState

tests/
├── conftest.py              # Shared fixtures (Column, DataModel, sample_assets)
├── test_fields.py           # 4 tests
├── test_asset.py            # 20 tests
├── test_dependency.py       # 7 tests
├── test_graph.py            # 13 tests
├── test_registry.py         # 12 tests
├── test_selectors.py        # 12 tests
├── test_ref_resolver.py     # 8 tests
├── test_lineage_resolver.py # 3 tests
├── test_compiled_cache.py   # 9 tests
├── test_project_loader.py   # 6 tests
├── test_state_models.py     # 8 tests
├── test_state_backends.py   # 13 tests
├── test_differ.py           # 7 tests
├── test_planner.py          # 6 tests
├── test_manager.py          # 12 tests
└── test_integration.py      # 7 tests — end-to-end workflows

demos/
├── 01_core_basics.py        # Assets, fingerprinting, graph, selectors, nested children
├── 02_plan_apply_workflow.py # Plan/apply/modify lifecycle
├── 03_multi_environment.py  # Shallow envs, promotion
├── 04_custom_loader.py      # YAML+SQL ProjectLoader subclass
├── 05_lineage_resolver.py   # Custom LineageResolver implementation
└── 06_ecommerce_platform.py # Full e-commerce platform with sqlglot lineage
```

## Future Considerations

These are not implemented but designed for:

### S3 + Tiered Backend (`state/s3.py`)
- `TieredBackend` = local mirror (fast reads) + remote S3 (writes, locking)
- `plan()` never hits S3 — always reads local mirror
- `apply()` writes to both local and S3
- `sync()` pulls remote → local (HEAD for ETag check, ~50ms)
- Chunked state for large environments (index + 500-asset chunks)

### Performance Targets
| Scenario | 5000 models |
|---|---|
| First ever run | ~2.5s |
| Cold start, nothing changed | ~160ms |
| Cold start, 3 files changed | ~165ms |
| Plan (nothing changed) | ~50ms |
| Plan (3 files changed) | ~55ms |

### Additional Selector Syntax
- Union operator (OR)
- Negation (`!tag:pii`)
- Path-based selectors
