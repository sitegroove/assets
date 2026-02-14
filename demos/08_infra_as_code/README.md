# Demo 08: Infrastructure as Code (Multi-Root Python Config)

This demo shows how to model infrastructure resources as typed Python config
objects and manage their state with the `assets` library.

It simulates a Terraform-style setup with two independent roots:

- `project/` for project-specific resources
- `shared_infra/` for shared platform resources

Each resource file is declarative only. It defines `Asset` objects but does not
perform provisioning.

## What this demo demonstrates

- Multi-root discovery with `FileDiscovery` and `SourceGroup`
- Consumer-owned parsing/import logic via a custom `ResourcesLoader`
- Dependency edges defined via `depends_on` and evaluated in `registry.graph`
- State lifecycle through `StateManager` (`plan` and `apply`)
- Fast repeat loads through discovery/index/state fast paths

## How this demo uses the assets library

The library handles orchestration and state; consumer code handles parsing:

- **Consumer-owned**
  - Resource models in `project/resource_models.py` and
    `shared_infra/infra_models.py`
  - Loader implementation in `loader.py` that imports modules and extracts
    `Asset` instances

- **Library-owned**
  - `Registry` for in-memory catalog and graph
  - `FileDiscovery` for scanning files and coordinating load/diff behavior
  - `StateManager` + SQLite backend for persisted state
  - Planning/apply workflows and graph traversal APIs

## Layout

- `main.py`: end-to-end demo flow (load, plan/apply, reload, impact checks)
- `loader.py`: custom loader used by `SourceGroup`
- `project/resources/*.py`: project resource declarations
- `shared_infra/resources/*.py`: shared infra declarations

## Run

From repo root:

```bash
python demos/08_infra_as_code/main.py
```

You should see:

- First load from both roots
- Plan/apply output
- Second load showing cache/state fast path behavior
- Basic impact analysis from graph lineage
