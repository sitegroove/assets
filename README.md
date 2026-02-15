# assets

A declarative, graph-based asset registry powered by Pydantic. Think Terraform for data assets: you declare what exists, the library handles state, diffing, and planning.

## Alpha Status

This library is in early alpha and currently private. Breaking changes may land
at any time without prior notice while the API and internals are still
stabilizing.

## Install

```bash
pip install assets

# Optional extras
pip install assets[yaml]    # YAML support (pyyaml)
pip install assets[s3]      # S3 remote state
pip install assets[cloud]   # S3 + GCS remote state
```

## Quick Start

```python
from assets import Asset, AssetField, Registry, StateManager, EnvironmentConfig, Environment

# 1. Define your asset type
class DataModel(Asset):
    row_count: int = AssetField(default=0, fingerprint=False)

# 2. Register assets
registry = Registry()
registry.register(DataModel(name="raw.users", kind="source", tags=["raw"]))
registry.register(DataModel(
    name="staging.users",
    kind="model",
    sql="SELECT * FROM raw.users",
    depends_on=["raw.users"],
))

# 3. Plan and apply
manager = StateManager.create(registry, local_path=".assets_state")
plan = manager.plan(environment="production")
plan.show()
result = manager.apply(plan)
```

## Import Guidance

- Prefer `from assets import ...` for stable day-to-day API usage.
- Use submodule imports (for example `assets.state.sqlite`) for advanced or
  backend-specific integration points.
- If you are building reusable internal wrappers, pin imports to the exact
  modules you depend on and test against new releases.

## Core Concepts

### Asset

A Pydantic `BaseModel` with automatic fingerprinting. Every asset has a `name`, optional `kind`, `tags`, `sql`, `description`, `metadata`, and nested `children`.

```python
class Column(Asset):
    type: str = ""
    pii: bool = False

class DataModel(Asset):
    row_count: int = AssetField(default=0, fingerprint=False)
```

### AssetField

Wraps Pydantic's `Field()` with a `fingerprint` flag. Fields marked `fingerprint=False` are excluded from change detection — useful for runtime stats like `row_count` or `last_synced_at`.

### Registry

Central store for assets and dependencies. Calling `register()` builds dependency edges from the asset's `depends_on` list and invalidates the lazy graph. Consumers are responsible for setting `depends_on` before registering.

```python
registry = Registry()
registry.register(asset)

# Query
registry.get("staging.users")
registry.all()
registry.select("tag:pii")
registry.graph  # lazy AssetGraph
```

### FileDiscovery + FileIndex

The library provides a fast file scanner (`FileDiscovery`) and a persistent index (`FileIndex`) for efficient change detection across 10,000+ model files. `FileIndex` uses two-tier freshness (mtime fast path, content hash fallback) and tracks per-entry dependencies (companion SQL, Jinja macros, variable files) so that changes to any dependency invalidate only the affected entries.

```python
from assets import FileDiscovery, FileIndex, SourceGroup
from assets.state.db import connect_state

# Discover files
discovery = FileDiscovery(groups=[SourceGroup(directory=Path("models"))])
result = discovery.discover()

# Classify against index (via StateManager)
manager = StateManager.create(registry, local_path=".assets_state")
index = manager.index  # FileIndex, ready to use
status = index.diff([(f.path, f.mtime_ns) for f in result.files], root)

# Fresh assets: load from state backend (last apply)
# status.fresh_names → ["raw.users", "staging.users", ...]
# Stale assets: re-parse from source
for path in status.stale:
    data = parse(path)
    index.put_file(path, root, asset_name=..., fingerprint=...,
                   deps=[(sql_path, "sql")])
```

## Consumer-Driven Loading

The library never touches files. Consumers own file discovery, parsing, and indexing — then register assets directly. This is the same pattern as Terraform: `.tf` files declare resources, Terraform handles state.

```python
from pathlib import Path
from assets import FileDiscovery, Registry, SourceGroup, StateManager

registry = Registry()
manager = StateManager.create(registry, local_path=".assets_state")
index = manager.index  # FileIndex, ready to use
backend = manager.backend
root = Path("./models")

# Discover and classify
discovery = FileDiscovery(groups=[SourceGroup(directory=root)])
result = discovery.discover()
status = index.diff([(f.path, f.mtime_ns) for f in result.files], root)

# Fresh entries — load from state backend (last apply)
snapshot = backend.load("production")
if snapshot is not None:
    for _path, asset_name in status.fresh:
        asset_state = snapshot.assets[asset_name]
        registry.register(DataModel.model_validate(asset_state.data))

# Stale entries — parse from source and update index
for path in status.stale:
    data = parse_file(path)
    asset = DataModel.model_validate(data)
    registry.register(asset)
    index.put_file(path, root, asset_name=asset.name,
                   fingerprint=asset.fingerprint)

# Handle deletions
registry.unregister_many([name for _, name in status.deleted])
```

For YAML + companion SQL files with dependency tracking:

```python
for path in status.stale:
    data = yaml.safe_load(path.read_text())
    sql_path = path.with_suffix(".sql")
    deps = []
    if sql_path.exists():
        data["sql"] = sql_path.read_text().strip()
        deps.append((sql_path, "sql"))
    asset = DataModel.model_validate(data)
    registry.register(asset)
    index.put_file(path, root, asset_name=asset.name,
                   fingerprint=asset.fingerprint, deps=deps)
```

## Plan/Apply Workflow

Terraform-style change detection. The library compares what's in the registry against persisted state to produce a plan.

```python
manager = StateManager.create(registry, local_path=".assets_state")

# Detect changes
plan = manager.plan(environment="production")
plan.show()  # Pretty-print changes

# Apply
result = manager.apply(plan)
# ApplyResult(created=3, updated=1, deleted=0)

# After file changes, re-scan and re-plan:
registry.clear()
# ... re-load files ...
plan2 = manager.plan(environment="production")
```

## Change Detection Layers

Each layer filters before the next, from cheapest to most expensive:

| Layer | What it checks | Cost |
|---|---|---|
| FileIndex mtime | File modification time | ~1us |
| FileIndex hash | File content SHA-256 | ~0.1us (cached) |
| FileIndex deps | Dependency file mtime/hash | ~1us per dep |
| Asset fingerprint | Fingerprinted fields | ~0.1us |
| Deep diff | Field-by-field comparison | ~1ms |
| Field deps (on-demand) | Column-level SQL analysis | ~10ms |

## Selectors

dbt-style query syntax for filtering assets:

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

```python
result = registry.select("tag:pii,kind:data_model")
names = result.names  # set of matching asset names
assets = result.assets  # list of matching Asset objects
```

## Multi-Environment

Supports full and shallow environments with parent inheritance:

| Type | What it stores | Use case |
|---|---|---|
| Full | Complete state of all assets | production, staging |
| Shallow | Only assets the developer touched | dev branches, PRs |

```python
manager = StateManager.create(
    registry,
    local_path=".assets_state",
    environments={
        "production": Environment(name="production"),
        "staging": Environment(name="staging", parent="production"),
    },
)

# Create ephemeral dev env
manager.create_environment("dev-alice", parent="production", shallow=True)

# Promote changes between environments
promote_plan = manager.promote(from_env="staging", to_env="production")
manager.apply(promote_plan)

# Clean up
manager.destroy_environment("dev-alice")
```

## Column-Level Dependencies

On-demand, consumer-implemented. The library provides the `DependencyResolver` abstract base class; consumers implement `resolve()` with their parser (e.g., sqlglot).

```python
from assets import Asset, DependencyResolver, FieldMapping, Registry

class SqlglotResolver(DependencyResolver):
    def resolve(self, asset: Asset, schema: dict[str, list[str]]) -> list[FieldMapping]:
        if not asset.sql:
            return []
        # Use sqlglot to trace column dependencies through SQL
        ...

registry = Registry(resolvers={"lineage": SqlglotResolver()})
mappings = registry.resolve("lineage", asset_id="mart.revenue")
```

## State Backends

| Backend | Use case |
|---|---|
| `SQLiteBackend` | Default, single-file persistence |
| `SQLiteBackend(":memory:")` | Testing, ephemeral (same schema/triggers as file) |
| `TieredBackend` | Local SQLite + remote S3/GCS sync |

```python
# SQLite (default via create())
manager = StateManager.create(registry, local_path=".assets_state")

# In-memory (testing) — same SQL schema and triggers as file-backed
from assets import SQLiteBackend
manager = StateManager(registry, SQLiteBackend.memory(), env_config)

# Remote sync (S3/GCS or local directory)
from assets import TieredBackend
backend = TieredBackend("s3://my-bucket/state")
backend = TieredBackend("/mnt/shared/state", local_path=".assets_state")
manager = StateManager(registry, backend, env_config)
```

## Extension Points

- **Asset subclasses** — add any Pydantic fields, use `AssetField(fingerprint=False)` to exclude from change detection, nest children via `children: list[Column]`
- **Dependency resolvers** — implement `DependencyResolver.resolve()` for SQL, Spark, pandas, etc.
- **State backends** — implement `StateBackend` for PostgreSQL, Redis, git-backed state, etc.
