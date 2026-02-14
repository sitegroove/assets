# Demo 09: Data Transformation (dbt-style with Jinja and sqlglot)

This demo shows a dbt-like analytics project built on top of the `assets`
library.

It combines:

- Jinja SQL rendering (`ref`, `source`, `var`, macros)
- YAML model/source/exposure metadata
- Column-level lineage resolution with `sqlglot`
- Multi-root loading (`project/` plus an external `modules/` package)

## What this demo demonstrates

- Source -> staging -> intermediate -> mart -> exposure asset graph
- Cross-module dependencies (project models depend on module models)
- Consumer-owned loaders for YAML + SQL parsing
- Nested column assets (`children`) with metadata like PII/classification
- On-demand field lineage via a named resolver:
  - `registry.add_resolver("lineage", ColumnLineageResolver())`
  - `registry.resolve("lineage", asset_id="...")`
- Full state workflow with `StateManager` (`plan`, `apply`, re-plan)
- Selector queries, impact analysis, and catalog-style introspection

## How this demo uses the assets library

- **Consumer-owned components**
  - Models in `models.py` (`DataModel`, `Column`, `Metric`, `Test`, `Owner`)
  - Loaders in `loader.py` (`SourceLoader`, `ModelLoader`, `ExposureLoader`)
  - Jinja + lineage utilities in `utils.py`

- **Library-owned components**
  - `Registry` for asset registration, selectors, and graph operations
  - `StateManager` and SQLite state backend for persisted environment state
  - Dependency graph operations (topological order, ancestors/descendants,
    stale impact sets)
  - Resolver protocol (`DependencyResolver`) for pluggable field-level
    dependency logic

## Layout

- `main.py`: orchestrates the full end-to-end flow
- `models.py`: typed asset/domain models
- `loader.py`: YAML/SQL loaders that return `LoadedAsset`
- `utils.py`: Jinja renderer and `ColumnLineageResolver`
- `project/`: local analytics project (sources, models, macros, exposures)
- `modules/`: external package-style analytics module

## Requirements

- `pyyaml` for YAML parsing
- `jinja2` for SQL template rendering
- `sqlglot` for column lineage extraction

## Run

From repo root:

```bash
python demos/09_data_transformation/main.py
```

The script prints each phase (setup, loading, graph exploration, selectors,
column lineage, plan/apply, impact analysis, test/metric catalogs).
