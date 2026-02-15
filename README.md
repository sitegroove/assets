# assets

A declarative, graph-based asset registry powered by Pydantic. Think Terraform
for data assets: you declare what exists, the library handles state, diffing,
and planning.

## Table of Contents

- [Features](#features)
- [Alpha Status](#alpha-status)
- [Installation](#installation)
- [Quick Start](#quick-start)
- [Core Concepts](#core-concepts)
  - [Defining Assets](#defining-assets)
  - [Plan / Apply Workflow](#plan--apply-workflow)
  - [Selectors](#selectors)
  - [Environments](#environments)
  - [File Loading](#file-loading)
- [Use Cases](#use-cases)
  - [Data Transformation Platform](#data-transformation-platform)
  - [Infrastructure as Code](#infrastructure-as-code)
- [Extension Points](#extension-points)
- [State Backends](#state-backends)
- [Advanced Usage](#advanced-usage)
- [Demos](#demos)
- [License](#license)

## Features

| Feature | What It Enables |
|---|---|
| **Declarative asset definitions** | Define assets as typed Python models with validation, IDE autocomplete, and custom fields. |
| **Automatic change detection** | Fingerprint-based diffing detects exactly what changed so you can skip unnecessary work. |
| **Plan/apply workflow** | Preview and review changes before writing state, enabling safe deploy and CI workflows. |
| **Dependency graph** | Build a DAG from declared relationships for impact analysis and topological execution order. |
| **Selector query language** | Target specific subsets of assets by tag, type, wildcard, lineage, or change state. |
| **Multi-environment state** | Isolate development and promotion flows with environment inheritance and protected targets. |
| **Pluggable file loading** | Bring your own parsing logic for YAML, JSON, Python, TOML, or external sources. |
| **Drift detection** | Compare current definitions against persisted state to catch unintended configuration drift. |
| **Column-level lineage hooks** | Add field-level dependency resolution for PII tracing and deep impact analysis. |
| **Remote state sync** | Share state through S3, GCS, or other fsspec backends while keeping fast local reads. |

## Alpha Status

This library is in early alpha and currently private. Breaking changes may land
at any time while APIs and internals are still stabilizing.

## Installation

```bash
pip install assets

# Optional extras
pip install assets[yaml]    # YAML support (pyyaml)
pip install assets[s3]      # S3 remote state
pip install assets[cloud]   # S3 + GCS remote state
```

## Quick Start

```python
from assets import Asset, AssetField, Project


class DataModel(Asset):
    row_count: int = AssetField(default=0, fingerprint=False)


project = Project(state_dir=".assets_state")

project.register(
    DataModel(
        id="raw.users",
        type="source",
        tags=["raw"],
    )
)

project.register(
    DataModel(
        id="staging.users",
        type="data_model",
        depends_on=["raw.users"],
    )
)

plan = project.plan()
plan.show()
result = project.apply(plan)

pii_assets = project.select("tag:pii")
impacted = project.select("state:modified+")
```

## Core Concepts

### Defining Assets

Assets are Pydantic models. Subclass `Asset` to add domain-specific fields.

```python
from assets import Asset, AssetField


class Column(Asset):
    type: str = ""
    pii: bool = False


class DataModel(Asset):
    row_count: int = AssetField(default=0, fingerprint=False)
```

Use `AssetField(fingerprint=False)` for operational fields that should not
trigger change detection.

```python
m1 = DataModel(id="example", row_count=0)
m2 = DataModel(id="example", row_count=999_999)
m1.fingerprint == m2.fingerprint  # True
```

Assets can also contain nested children for column-level or sub-resource
modeling.

```python
class Column(Asset):
    type: str = ""
    pii: bool = False


class DataModel(Asset):
    columns: list[Column] = AssetField(default_factory=list, children=True)


project.register(
    DataModel(
        id="raw.users",
        type="source",
        columns=[
            Column(id="user_id", type="INTEGER"),
            Column(id="email", type="VARCHAR", pii=True),
        ],
    )
)

users = project.get("raw.users")
users.children()
users.child("columns/email").pii
```

### Plan / Apply Workflow

`plan()` compares your registered assets with persisted state and returns a
preview of creates, updates, and deletes.

```python
project = Project(state_dir=".assets_state")

# ... register assets ...

plan = project.plan()
plan.show()

result = project.apply(plan)
# ApplyResult(created=2, updated=1, deleted=1)

next_plan = project.plan()
next_plan.has_changes  # False
```

### Selectors

Use selector expressions to query subsets of assets.

| Pattern | Example | Behavior |
|---|---|---|
| Exact name | `staging.users` | Single asset |
| Tag filter | `tag:pii` | All with tag |
| Type filter | `type:data_model` | All with type |
| Wildcard | `raw.*` | Glob match on id |
| Upstream | `+staging.users` | Asset + all ancestors |
| Downstream | `staging.users+` | Asset + all descendants |
| Both | `+staging.users+` | Ancestors + self + descendants |
| Depth-limited | `staging.users+2` | Descendants up to depth 2 |
| Intersection | `tag:pii,type:data_model` | AND of multiple selectors |
| State modified | `state:modified` | Created + updated + deleted ids |
| State created | `state:created` | Only newly created ids |
| State updated | `state:updated` | Only updated ids |
| State deleted | `state:deleted` | Only deleted ids |

```python
pii = project.select("tag:pii")
impacted = project.select("state:modified+")
```

### Environments

Model development and promotion flows with full and shallow environments.

```python
from assets import Environment, EnvironmentConfig, Project


project = Project(
    environment="production",
    state_dir=".assets_state",
    env_config=EnvironmentConfig(
        default="production",
        environments={
            "production": Environment(name="production"),
            "staging": Environment(name="staging", parent="production"),
        },
    ),
    protected_environments={"production", "staging"},
)

project.create_environment("dev-alice", parent="production", shallow=True)
promote_plan = project.promote_to("staging")
project.apply(promote_plan)
```

| Type | What it stores | Use case |
|---|---|---|
| Full | Complete state of all assets | production, staging |
| Shallow | Only assets changed in that environment | dev branches, PRs |

### File Loading

Consumers own parsing logic. The library handles discovery, freshness checks,
rehydration from state, and registration.

```python
from pathlib import Path

import yaml

from assets import Asset, LoadedAsset, Project, SourceGroup


class DataModel(Asset):
    pass


class YamlLoader:
    def load(self, path: Path, root: Path) -> list[LoadedAsset]:
        data = yaml.safe_load(path.read_text())
        asset = DataModel.model_validate(data)
        return [LoadedAsset(asset=asset)]


project = Project(state_dir=".assets_state")
result = project.load(
    [
        SourceGroup(
            directory=Path("models"),
            patterns=["*.yaml"],
            loader=YamlLoader(),
        )
    ]
)

print(result.summary())
```

## Use Cases

### Data Transformation Platform

Use `assets` as the registry and state engine behind dbt-style analytics
projects with SQL models, tags, tests, and lineage.

```python
from pathlib import Path

from assets import Project, SourceGroup


project = Project(state_dir=".assets_state")

# Load model definitions from multiple roots (project + modules)
result = project.load(
    [
        SourceGroup(
            directory=Path("project/models"),
            patterns=["*.yml"],
            loader=ModelLoader(),
        ),
        SourceGroup(
            directory=Path("modules/models"),
            patterns=["*.yml"],
            loader=ModelLoader(),
        ),
    ]
)

build_order = project.graph.topological_sort()
impact = project.select("raw.users+")

plan = project.plan()
project.apply(plan)
```

See `demos/09_data_transformation` for a complete end-to-end example with
Jinja rendering, refs/sources, and sqlglot lineage hooks.

### Infrastructure as Code

Use typed Python config files to model infrastructure resources and dependency
graphs for shared and project-specific stacks.

```python
from pathlib import Path

from assets import Project, SourceGroup


project = Project(state_dir=".assets_state")

result = project.load(
    [
        SourceGroup(
            directory=Path("project/resources"),
            patterns=["*.py"],
            loader=ResourceLoader(),
        ),
        SourceGroup(
            directory=Path("shared_infra/resources"),
            patterns=["*.py"],
            loader=ResourceLoader(),
        ),
    ]
)

roots = project.graph.roots()
leaves = project.graph.leaves()

plan = project.plan()
project.apply(plan)
```

See `demos/08_infra_as_code` for a complete multi-root infrastructure example.

## Extension Points

- **Asset subclasses**: add domain-specific fields, nested children, and
  fingerprint controls.
- **Custom loaders**: implement `load(path, root) -> list[LoadedAsset]` for
  your source format.
- **Dependency resolvers**: implement `DependencyResolver` for on-demand,
  field-level lineage.
- **State backends**: implement `StateBackend` for custom persistence layers.

```python
from assets import DependencyResolver, FieldMapping


class SqlResolver(DependencyResolver):
    def resolve(self, asset, schema):
        return [
            FieldMapping(
                source="raw.users/email",
                target="staging.users/email_clean",
            )
        ]


project.add_resolver("lineage", SqlResolver())
mappings = project.resolve("lineage", asset_id="staging.users")
```

## State Backends

| Backend | Use case |
|---|---|
| `SQLiteBackend` | Default, single-file local persistence |
| `SQLiteBackend.memory()` | In-memory backend for tests and short-lived runs |
| `TieredBackend` | Local SQLite + remote sync through S3/GCS/other fsspec stores |

```python
from assets import Project, TieredBackend


project = Project(state_dir=".assets_state")
remote_project = Project(backend=TieredBackend("s3://my-bucket/state"))
```

## Advanced Usage

`Project` is the recommended facade for day-to-day usage. When needed, you can
drop down to lower-level control through escape hatches:

```python
project.registry
project.manager
project.graph
```

For internal architecture, data flow, and low-level APIs, see
`ARCHITECTURE.md`.

## Demos

| Demo | Description | Complexity |
|---|---|---|
| `demos/01_core_basics` | Defining assets, fingerprinting, graph queries, selectors | Beginner |
| `demos/02_plan_apply_workflow` | Terraform-style plan/apply lifecycle | Beginner |
| `demos/03_multi_environment` | Shallow dev environments and promotion | Intermediate |
| `demos/04_custom_loader` | Consumer-driven YAML+SQL loading with `SourceGroup` | Intermediate |
| `demos/05_lineage_resolver` | Custom field-level dependency resolver | Intermediate |
| `demos/06_ecommerce_platform` | Full e-commerce analytics architecture | Advanced |
| `demos/07_terraform_style_python_configs` | Terraform-style Python configuration project | Intermediate |
| `demos/08_infra_as_code` | Multi-root infrastructure as code workflow | Advanced |
| `demos/09_data_transformation` | dbt-style data transformation architecture | Advanced |

## License

See `LICENSE`.
