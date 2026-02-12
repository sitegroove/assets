# Assets

A **declarative, graph-based asset registry** powered by Pydantic, sitting at the intersection of data cataloging (like DataHub/Amundsen) and infrastructure-as-code (like Terraform/Pulumi).

## Core Capabilities

- Register Pydantic-based assets (data models, sources, dashboards, etc.) into a directed acyclic graph
- Automatic dependency extraction from SQL via regex `{{ ref('...') }}` patterns
- Terraform-style **plan/apply/drift** workflow with fingerprint-based diffing
- Multi-environment state (production, staging, shallow dev) with promotion flow
- dbt-style **selectors** for querying the asset graph
- Abstract **lineage resolver** base class for consumer-provided column-level lineage
- **Per-file compiled cache** for fast cold starts

## Requirements

- Python 3.11+
- pydantic v2
- pyyaml

## Installation

```bash
pip install -e ".[dev]"
```

## Quick Start

### 1. Define Your Asset Types

```python
from pydantic import BaseModel
from assets import Asset, AssetField

class Column(BaseModel):
    name: str
    type: str = ""
    description: str = ""
    pii: bool = False

class DataModel(Asset):
    columns: list[Column] = AssetField(default_factory=list, field_source=True)
    row_count: int = AssetField(default=0, fingerprint=False)  # excluded from fingerprint
```

### 2. Register Assets

```python
from assets import Registry

registry = Registry()

# Sources (no SQL)
registry.register(DataModel(
    name="raw.users",
    kind="source",
    tags=["raw"],
    columns=[
        Column(name="user_id", type="INTEGER"),
        Column(name="email", type="VARCHAR", pii=True),
    ],
))

# Models with SQL — dependencies extracted automatically
registry.register(DataModel(
    name="staging.users",
    kind="data_model",
    tags=["staging", "pii"],
    sql="SELECT u.user_id, LOWER(TRIM(u.email)) AS email_clean FROM {{ ref('raw.users') }} u",
    columns=[
        Column(name="user_id", type="INTEGER"),
        Column(name="email_clean", type="VARCHAR", pii=True),
    ],
))
```

### 3. Query the Graph

```python
# Traversal
graph = registry.graph
graph.ancestors("staging.users")      # {"raw.users"}
graph.descendants("raw.users")        # {"staging.users"}
graph.roots()                         # {"raw.users"}
graph.topological_sort()              # ["raw.users", "staging.users"]

# Selectors (dbt-style)
registry.select("tag:pii")           # all PII assets
registry.select("kind:source")       # all sources
registry.select("raw.*")             # wildcard match
registry.select("+staging.users")    # asset + all ancestors
registry.select("staging.users+")   # asset + all descendants
registry.select("raw.users+1")      # descendants up to depth 1
registry.select("tag:pii,kind:data_model")  # intersection (AND)
```

### 4. Plan/Apply Workflow

```python
from assets import (
    Registry, ProjectLoader, StateManager,
    EnvironmentConfig, Environment, LocalJSONBackend,
)

registry = Registry()
loader = ProjectLoader(registry, asset_class=DataModel)
backend = LocalJSONBackend()
config = EnvironmentConfig(
    default="development",
    environments={
        "production": Environment(name="production"),
        "staging": Environment(name="staging", parent="production"),
        "development": Environment(name="development", parent="production", shallow=True),
    },
)
manager = StateManager(registry, loader, backend, config)

# Detect changes
plan = manager.plan("./models", environment="production")
print(plan.show())

# Apply changes
result = manager.apply(plan, environment="production")
print(f"Created: {result.created}, Updated: {result.updated}, Deleted: {result.deleted}")

# Promote across environments
promote_plan = manager.promote(from_env="production", to_env="staging")
manager.apply(promote_plan, environment="staging")
```

### 5. Field Introspection

```python
asset = registry.get("staging.users")
asset.list_fields()          # ["user_id", "email_clean"]
col = asset.get_field("email_clean")
col.pii                      # True
```

## Architecture

### Package Structure

```
assets/
├── core/
│   ├── fields.py         # AssetField() metadata wrapper
│   ├── asset.py          # Base Asset model + fingerprinting
│   ├── dependency.py     # Dependency + FieldMapping models
│   ├── graph.py          # AssetGraph (DAG, traversal, selectors)
│   └── registry.py       # Registry (central store)
├── loader/
│   ├── compiled.py       # CompiledCache (per-file persistent cache)
│   └── project.py        # ProjectLoader (extensible)
├── resolver/
│   ├── ref.py            # RefResolver (regex {{ ref('x') }})
│   └── lineage.py        # LineageResolver (abstract base class)
├── selector/
│   └── parser.py         # dbt-style selector parser
├── state/
│   ├── models.py         # StateSnapshot, AssetState, etc.
│   ├── environment.py    # Environment config
│   ├── backend.py        # StateBackend ABC
│   ├── memory.py         # MemoryBackend (testing)
│   └── local.py          # LocalJSONBackend (file-based)
└── engine/
    ├── differ.py         # Fingerprint-first diffing
    ├── planner.py        # Plan model + pretty print
    └── manager.py        # StateManager (plan/apply/drift/promote)
```

### Key Design Principles

1. **Fingerprints everywhere** — every asset has a deterministic SHA-256 hash for fast diffing
2. **Separate read/write paths** — `plan()` is fast (local only), `apply()` acquires locks
3. **On-demand column lineage** — never runs automatically; consumer provides their own resolver
4. **Per-file compiled cache** — mtime fast path, content hash fallback, corruption-safe
5. **Shallow environments** — dev envs store only overrides, inherit from parent
6. **Dependencies live on the graph** — assets declare SQL, library extracts refs
7. **Extensible loader** — consumers subclass `ProjectLoader` for YAML, Python, etc.

### AssetField Metadata

`AssetField()` wraps Pydantic's `Field()` with additional metadata:

| Declaration | Fingerprinted | Field Source |
|---|---|---|
| `AssetField()` | Yes | No |
| `AssetField(field_source=True)` | Yes | Yes |
| `AssetField(fingerprint=False)` | No | No |
| `AssetField(fingerprint=False, field_source=True)` | No | Yes |
| Plain `Field()` or bare attribute | Yes | No |

### Selector Syntax

| Selector | Meaning |
|---|---|
| `staging.users` | Exact match |
| `tag:pii` | All assets with tag "pii" |
| `kind:data_model` | All assets with kind "data_model" |
| `+staging.users` | Asset + all ancestors (upstream) |
| `staging.users+` | Asset + all descendants (downstream) |
| `+staging.users+` | Asset + ancestors + descendants |
| `raw.*` | Wildcard name match |
| `tag:pii,kind:data_model` | Intersection (AND) |
| `staging.users+2` | Descendants up to depth 2 |

### Environments

| Type | Shallow | Stores | Description |
|---|---|---|---|
| production | No | All assets | Full state |
| staging | No | All assets | Mirror of production |
| dev_alice | Yes | Only overrides | Inherits from parent |
| pr-142 | Yes | Only overrides | Ephemeral, disposable |

Shallow environments store only the assets a developer touched. For everything else, they read from their parent. Deletions are stored as tombstones so they don't fall through to the parent.

## Extending the Library

### Custom File Loader (e.g., YAML)

```python
import yaml
from pathlib import Path
from typing import Any
from assets import ProjectLoader

class YAMLLoader(ProjectLoader):
    def parse_file(self, path: Path, root: Path) -> dict[str, Any] | None:
        if path.suffix not in (".yaml", ".yml"):
            return None
        data = yaml.safe_load(path.read_text())
        # Optionally merge SQL from companion .sql file
        sql_path = path.with_suffix(".sql")
        if sql_path.exists():
            data["sql"] = sql_path.read_text()
        return data

    def discover_files(self, project_dir: Path) -> list[Path]:
        return sorted(
            p for p in project_dir.rglob("*")
            if p.suffix in (".yaml", ".yml")
        )
```

### Custom Lineage Resolver (e.g., sqlglot)

```python
from assets import LineageResolver, FieldMapping

class SqlglotLineageResolver(LineageResolver):
    def resolve(self, sql: str, schema: dict[str, list[str]]) -> list[FieldMapping]:
        import sqlglot
        from sqlglot.lineage import lineage

        mappings = []
        # ... use sqlglot to trace column lineage ...
        return mappings

# Usage
resolver = SqlglotLineageResolver()
lineage = registry.resolve_field_dependency(
    asset_name="staging.users",
    resolver=resolver,
)
```

## Development

```bash
# Install dev dependencies
pip install -e ".[dev]"

# Run tests
pytest

# Run linter
ruff check assets/ tests/

# Run tests with coverage
pytest --cov=assets
```

## License

See [LICENSE](LICENSE) for details.
