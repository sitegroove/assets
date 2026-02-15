# Architecture

This document describes the internal architecture of the `assets` library — its module structure, data flow, extension points, and design rationale. Use this as a reference when contributing or building on top of the library.

## High-Level Overview

```
┌──────────────────────────────────────────────────────────────────┐
│                        Consumer Code                             │
│  (defines Asset subclasses, loaders, dependency resolvers)        │
└──────────────┬───────────────────────────────────────────────────┘
               │
┌──────────────▼───────────────────────────────────────────────────┐
│                      assets (this library)                        │
│                                                                   │
│  ┌─────────┐  ┌──────────┐  ┌──────────┐  ┌──────────────────┐  │
│  │  Core   │  │ Resolver │  │  Loader  │  │     Engine       │  │
│  │         │  │          │  │          │  │                  │  │
│  │  Asset   │  │ Dep      │  │ Discovery│  │ Differ           │  │
│  │  Fields  │  │ Resolver │  │ FileIndex│  │ Planner          │  │
│  │  Dep     │  │ (ABC)    │  │ (SQLite) │  │ StateManager     │  │
│  │  Graph   │  └──────────┘  │          │  │ (plan/apply/     │  │
│  │  Registry│                │          │  │  drift/promote)  │  │
│  └─────────┘                └──────────┘  └──────────────────┘  │
│                                                                   │
│  ┌───────────┐  ┌──────────────────────────────────────────────┐ │
│  │ Selector  │  │              State                           │ │
│  │           │  │ Models | Environments | Backend (ABC)        │ │
│  │ Graph/    │  │ SQLiteBackend (:memory: | file) | TieredBackend│ │
│  │ State     │  └──────────────────────────────────────────────┘ │
│  └───────────┘                                                   │
│                                                                   │
│  ┌──────────────────────────────────────────────────────────────┐ │
│  │ Project Facade (high-level API)                              │ │
│  │ register/select/plan/apply/promote_to/load                 │ │
│  └──────────────────────────────────────────────────────────────┘ │
└──────────────────────────────────────────────────────────────────┘
```

## Module Map

### `assets/core/` — Foundation Models

Everything in the library builds on these models.

| File | Key Types | Purpose |
|---|---|---|
| `fields.py` | `AssetField()` | Pydantic `Field()` wrapper with `fingerprint` metadata |
| `asset.py` | `Asset` | Base model: id, type, tags, metadata. Computed `fingerprint` (SHA-256). Child introspection via `get_child()`, `list_children()`, `get_child_at()`. Consumers define children via `AssetField(children=True)` on subclasses |
| `dependency.py` | `Dependency`, `FieldMapping` | Graph edge (source→target) with type and computed fingerprint. FieldMapping uses path-based `source`/`target` (e.g., `"raw.users/email"`) with `/` separator |
| `graph.py` | `AssetGraph`, `SelectionResult` | DAG built from assets + dependencies. Traversal (`ancestors`, `descendants`), `topological_sort()`, `roots()`, `leaves()`, `stale()` for topology-aware cascade |
| `registry.py` | `Registry` | Central store. `register()`, `register_many()`, `unregister()`, `unregister_many()`. Lazy graph. Delegates selector queries and field-level dependency resolution |

**Data flow:**
```
Consumer sets asset.depends_on = ["raw.users", ...]
Asset created → Registry.register()
  → Dependency objects built from depends_on
  → graph invalidated (rebuilt lazily)
```

The library never inspects SQL content. Consumers are responsible for setting `depends_on` on each asset before registering (e.g., by parsing `{{ ref() }}` templates in their loading code).

### `assets/resolver/` — Field-Level Dependency Resolution

| File | Key Types | Purpose |
|---|---|---|
| `lineage.py` | `DependencyResolver` (ABC) | Abstract base class. Consumers implement `resolve(sql, schema) → list[FieldMapping]` with their own parser (e.g., sqlglot for SQL, custom parsers for other formats) |

**Field-level dependency resolution is never automatic.** It is explicitly requested by the consumer via `registry.resolve_field_dependency()`, passing their own resolver instance.

### `assets/selector/` — Query Language

| File | Key Types | Purpose |
|---|---|---|
| `base.py` | `Selector` (ABC) | Abstract selector with built-in state awareness helpers (`_state_names`, `_resolve_plan`) |
| `parser.py` | `GraphSelector` | Parse and execute selectors against a registry graph — handles graph traversal, tag/type filters, wildcards, and `state:*` terms |

**Supported syntax:**

| Pattern | Example | Behavior |
|---|---|---|
| Exact name | `staging.users` | Single asset |
| Tag filter | `tag:pii` | All with tag |
| Type filter | `type:data_model` | All with type |
| Wildcard | `raw.*` | Glob match on name |
| Upstream | `+staging.users` | Asset + all ancestors |
| Downstream | `staging.users+` | Asset + all descendants |
| Both | `+staging.users+` | Ancestors + self + descendants |
| Depth-limited | `staging.users+2` | Descendants up to depth 2 |
| Intersection | `tag:pii,kind:data_model` | AND of multiple selectors |

`GraphSelector` uses regex to detect graph traversal patterns (`+name+2`) and delegates traversal to `AssetGraph.ancestors()`/`descendants()` with optional `max_depth`.

### `assets/index/` — File Change Detection and Indexing

| File | Key Types | Purpose |
|---|---|---|
| `base.py` | `Index` (ABC) | Abstract base class for index implementations. Methods: `put()`, `remove()`, `stale_entries()`, `close()`. Freshness tracker only — no data storage or retrieval |
| `file.py` | `FileIndex`, `IndexStatus`, `MtimeCache` | SQLite-backed file index with per-entry dependency tracking. Two-tier freshness: mtime → content hash → miss. Hash/fingerprint in `index.db` (local only); mtimes in local JSON cache (never synced) |

### `assets/loader/` — File Discovery

| File | Key Types | Purpose |
|---|---|---|
| `discovery.py` | `FileDiscovery`, `SourceGroup`, `DiscoveredFile`, `DiscoveryResult` | Fast file scanner using `os.scandir()`. Supports multiple directory groups, glob patterns, exclusions, and regex filters |

The library does not own file loading. Consumers discover files with `FileDiscovery`,
classify them against `FileIndex`, parse stale files, and register assets directly.

**Index check flow:**
```
1. FileDiscovery.discover() → list of (path, mtime_ns)
2. FileIndex.diff(discovered, root) → IndexStatus
   For each file:
     Not in index?                  → new (parse needed)
     mtime matches (local cache)?   → check deps
     Content hash matches?          → update local mtime cache, check deps
     Neither?                       → changed (parse needed)
   For deps:
     dep mtime matches (local)?     → fresh
     dep hash matches?              → fresh (update local mtime cache)
     dep deleted or changed?        → parent entry is stale
   Entries not in discovered?       → deleted
3. Fresh entries: load from state backend by asset name
4. Stale entries: parse, register, index.put_file() with deps
5. Deleted entries: registry.unregister_many()
```

**On-disk layout:**
Index tables (`index_entries`, `index_deps`) live in a separate `index.db` (never synced
to remote). This keeps the file index local-only, while `state.db` holds only environment
state. Mtimes are stored in a **local JSON file** (`mtime_cache.json`, never synced) so
that mtime-only changes (save/revert, git checkout) do not dirty any database and trigger
unnecessary remote pushes via `TieredBackend`.

### `assets/state/` — Persistence and Environments

| File | Key Types | Purpose |
|---|---|---|
| `models.py` | `StateSnapshot`, `AssetState`, `DependencyState` | Pydantic models for persisted state |
| `environment.py` | `Environment`, `EnvironmentConfig` | Environment definition (name, metadata) and multi-environment config |
| `backend.py` | `StateBackend` (ABC) | Abstract interface: `load()`, `save()`, `lock()`, `list_environments()`, `delete_environment()`, `copy_environment()` |
| `db.py` | `connect_state`, `connect_index` | SQLite schema definitions (separate state and index schemas), connection factories, WAL pragmas |
| `sqlite.py` | `SQLiteBackend` | Default backend. Directory-per-environment with one `state.db` per env. Supports `":memory:"` for fast, transient testing. `copy_environment()` uses SQLite file copy with WAL checkpoint |
| `tiered.py` | `TieredBackend` | Local SQLite + remote sync via fsspec. Per-environment remote paths (`{root}/{env}/state.db`). `copy_environment()` syncs source, copies local, pushes target |

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
│       ├── applied_at, applied_by
│       └── version: int
├── dependencies: list[DependencyState]
└── metadata: dict
```

**Environment model:**

Each environment is fully independent — it stores a complete copy of all
asset state. Creating a new environment copies the parent's `state.db` file
(copy-on-create). From that point on the two environments share nothing.
There are no shallow environments, parent chains, or tombstones.

**Backend on-disk layout (SQLiteBackend):**
```
.assets_state/
├── index.db                     ← file index (never synced to remote)
├── production/
│   └── state.db                 ← production environment state
├── staging/
│   └── state.db                 ← staging environment state
└── dev-alice/
    └── state.db                 ← dev environment state (copied from production)
```

**Backend on-disk layout (TieredBackend — local + remote):**
```
Local (base_path):
  .assets_state/
  ├── index.db                   ← file index (local only)
  ├── production/
  │   └── state.db               ← local SQLite (fast reads)
  └── dev-alice/
      └── state.db

Remote (remote_root — S3/GCS or local dir):
  s3://my-bucket/state/
  ├── production/
  │   ├── state.db               ← full SQLite database
  │   └── snapshot.json          ← sync metadata (fingerprint, version)
  ├── dev-alice/
  │   ├── state.db
  │   └── snapshot.json
  └── production.lock            ← temporary lock file
```

### `assets/engine/` — Plan/Apply/Drift/Promote

| File | Key Types | Purpose |
|---|---|---|
| `differ.py` | `Differ`, `Change`, `ChangeSet`, `FieldChange` | Fingerprint-first comparison. Deep field-level diff only when fingerprints differ |
| `planner.py` | `Plan` | Plan model wrapping a ChangeSet. `show()` for pretty-print. `has_changes` property |
| `manager.py` | `StateManager`, `ApplyResult`, `ResolvedState` | Orchestrator: `plan()`, `apply()`, `drift()`, `promote_to()`, `create_environment()`, `destroy_environment()` |

**Differ algorithm:**
```
For each desired asset:
  1. Not in current state?          → CREATE
  2. Fingerprint matches?           → SKIP (fast path)
  3. Fingerprint differs?           → UPDATE + deep diff fields

For each asset in state but not desired:
  → DELETE
```

**StateManager.plan() flow:**
```
plan(selector, environment?)
  1. registry.all() or GraphSelector(registry).execute(selector)  ← desired assets
  2. backend.load(env)                    ← load state from env's state.db
  3. differ.diff(desired, current)        ← fingerprint-first
  4. return Plan(changeset)
```

Consumers own file loading: discover → parse → validate → cache → register.
The library operates on whatever is currently in the registry.

**StateManager.apply() flow:**
```
apply(plan, environment?)
  with backend.lock(env):               ← context manager acquires/releases lock
    1. backend.load(env)                ← get current state
    2. for each change:
       - CREATE: add AssetState
       - UPDATE: replace AssetState
       - DELETE: remove from state
    3. backend.save(env, state)         ← persist (+ push to remote for Tiered)
  return ApplyResult
```

**StateManager.promote_to() flow:**
```
promote_to(to_env, selector, from_env?)
  1. Load source env state
  2. Load target env state
  3. Build desired from source (optionally filtered)
  4. Diff desired vs. target
  5. Return Plan for target env
```

**TieredBackend sync flow:**
```
Sync protocol (per-environment):
  1. Remote stores per env: {root}/{env}/state.db + {root}/{env}/snapshot.json
  2. snapshot.json = {"fingerprint": "<sha256>", "version": N, "updated_at": "..."}
  3. On load(env): compare local DB fingerprint vs remote snapshot for that env
     - Match → skip download (fast local path)
     - Differ → pull remote state.db → local env directory
  4. On save(env): write local SQLite, then push state.db + snapshot.json for env
  5. On lock(env): acquire remote lock file, pull if stale, yield, push on exit
  6. copy_environment(src, tgt): sync source, file-copy local state.db, push target
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
- Use `AssetField(children=True)` to declare child asset fields (e.g., `columns: list[Column]`)
- Children are namespaced under the parent with auto-generated paths

### 2. Consumer-Driven Loading

Consumers own file discovery, parsing, and indexing using `FileDiscovery` + `FileIndex`:
- JSON files with `json.loads()`
- YAML + companion SQL files (with dependency tracking)
- Python modules with decorators
- TOML configuration
- Database introspection

### 3. Dependency Resolvers (`DependencyResolver`)

Implement `resolve(sql, schema) → list[FieldMapping]`:
- sqlglot-based SQL column tracing
- Spark DataFrame lineage parser
- pandas operation chain analyzer
- YAML pipeline config resolver
- External catalog integration

### 4. State Backends (`StateBackend`)

Implement the abstract methods for any storage. Built-in backends:
- `SQLiteBackend` — directory-per-environment with one `state.db` per env (default); pass `":memory:"` for testing
- `TieredBackend` — local SQLite + remote S3/GCS/Azure sync

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
| FileIndex mtime | File modification time | ~1us | "might have changed" |
| FileIndex hash | File content SHA-256 | ~0.1us (cached) | "file content changed" |
| FileIndex deps | Dependency file mtime/hash | ~1us per dep | "dependency changed" |
| Asset fingerprint | Fingerprinted fields | ~0.1us | "asset definition changed" |
| Deep diff | Field-by-field comparison | ~1ms | "what exactly changed" |
| Field deps (on-demand) | Column-level SQL analysis | ~10ms | "which columns affected" |

## Data Model Relationships

```
Registry
├── _assets: dict[str, Asset]           ← name → asset
├── _dependencies: list[Dependency]     ← edges (from depends_on)
└── _graph: AssetGraph (lazy)
    ├── _assets: dict[str, Asset]
    ├── _forward: dict[str, set[str]]   ← parent → children
    └── _backward: dict[str, set[str]]  ← child → parents

StateManager
├── registry: Registry
├── backend: StateBackend
│   └── SQLiteBackend (:memory: | file) | TieredBackend
└── env_config: EnvironmentConfig
    └── environments: dict[str, Environment]
```

## Key Invariants

1. **`register()` builds dependencies from `depends_on`** — consumers set this before registering; the library never inspects SQL
2. **Graph is lazily built** — invalidated on every `register()`, rebuilt on first access
3. **Fingerprints are computed, never stored** on the Asset — they're derived from field values
4. **State stores fingerprints** — `AssetState.fingerprint` is the hash at apply time
5. **Plan never mutates state** — it only reads and compares
6. **Apply always acquires a lock** — concurrent applies to the same env are serialized
7. **Environments are fully independent** — each has its own `state.db`; no parent chains or tombstones
8. **Protected environments cannot be destroyed** — `production` and `staging` are protected
9. **Field-level dependency resolution never runs automatically** — only when explicitly called by the consumer
10. **SQLite is the default** — all local persistence uses SQLite (index.db + per-env state.db)
11. **Tiered sync uses snapshot fingerprint** — per-env comparison avoids unnecessary remote downloads
12. **index.db is local-only** — file index tables live in a separate database, never synced to remote

## File Inventory

```
assets/
├── __init__.py              # Public API exports
├── core/
│   ├── __init__.py
│   ├── fields.py            # AssetField(), FINGERPRINT_KEY
│   ├── asset.py             # Asset (BaseModel), _serialize_value(), model_rebuild()
│   ├── dependency.py        # Dependency, FieldMapping
│   ├── graph.py             # AssetGraph, SelectionResult
│   └── registry.py          # Registry
├── index/
│   ├── __init__.py
│   ├── base.py              # Index (ABC)
│   └── file.py              # FileIndex, IndexStatus
├── loader/
│   ├── __init__.py
│   └── discovery.py         # FileDiscovery, SourceGroup, DiscoveredFile
├── resolver/
│   ├── __init__.py
│   └── lineage.py           # DependencyResolver (ABC)
├── selector/
│   ├── __init__.py
│   ├── base.py              # Selector (ABC) with state awareness helpers
│   └── parser.py            # GraphSelector (graph + state selectors)
├── state/
│   ├── __init__.py
│   ├── models.py            # StateSnapshot, AssetState, DependencyState
│   ├── environment.py       # Environment, EnvironmentConfig
│   ├── backend.py           # StateBackend (ABC)
│   ├── db.py                # SQLite schemas (state + index), connection factories, WAL pragmas
│   ├── sqlite.py            # SQLiteBackend (default)
│   └── tiered.py            # TieredBackend (local SQLite + remote sync)
└── engine/
    ├── __init__.py
    ├── differ.py            # Differ, Change, ChangeSet, FieldChange
    ├── planner.py           # Plan
    └── manager.py           # StateManager, ApplyResult, ResolvedState

tests/
├── conftest.py              # Shared fixtures (Column, DataModel, sample_assets)
├── test_fields.py           # AssetField tests
├── test_asset.py            # Asset model tests
├── test_dependency.py       # Dependency + FieldMapping tests
├── test_graph.py            # AssetGraph tests
├── test_registry.py         # Registry tests
├── test_selectors.py        # Selector parser tests
├── test_dependency_resolver.py # DependencyResolver tests
├── test_index_base.py       # Index ABC tests
├── test_file_index.py       # FileIndex tests
├── test_discovery.py        # FileDiscovery tests
├── test_state_models.py     # State model tests
├── test_state_backends.py   # SQLite in-memory, SQLite file, Tiered backend tests
├── test_sqlite_backend.py   # SQLiteBackend detailed tests + history
├── test_review_edge_cases.py # Locking edge cases, version increment, dependency history
├── test_differ.py           # Differ tests
├── test_planner.py          # Plan tests
├── test_manager.py          # StateManager tests
├── test_ux_improvements.py  # Factory methods, repr, error handling
└── test_integration.py      # End-to-end workflows

demos/
├── 01_core_basics/
│   └── main.py              # Project, fingerprinting, graph, selectors, nested children
├── 02_plan_apply_workflow/
│   └── main.py              # Plan/apply/modify lifecycle
├── 03_multi_environment/
│   └── main.py              # Copy-on-create envs, promotion
├── 04_custom_loader/
│   └── main.py              # Consumer-driven YAML+SQL loading with FileDiscovery + FileIndex
├── 05_lineage_resolver/
│   └── main.py              # Custom DependencyResolver implementation
├── 06_ecommerce_platform/
│   └── main.py              # Full e-commerce platform with sqlglot dependency resolution
├── 07_terraform_style_python_configs/
│   ├── main.py              # Terraform-style Python config workflow
│   └── terraform_style_project/ # Multi-file config-only resource declarations
└── 08_fake_google_cloud_resources/
    ├── main.py              # Fake GCP config-only resources workflow
    └── gcp_config_project/  # Multi-file GCP resource declarations

benchmarks/
├── bench_500_assets.py      # SQLite(:memory:) vs SQLite(file) performance benchmarks
└── bench_column_lineage.py  # Column-level lineage resolver benchmarks
```

## Performance Targets

| Scenario | 5000 models |
|---|---|
| First ever run | ~2.5s |
| Cold start, nothing changed | ~160ms |
| Cold start, 3 files changed | ~165ms |
| Plan (nothing changed) | ~50ms |
| Plan (3 files changed) | ~55ms |

### Additional Selector Syntax (Future)
- Union operator (OR)
- Negation (`!tag:pii`)
- Path-based selectors
