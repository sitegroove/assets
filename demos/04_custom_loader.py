#!/usr/bin/env python3
"""Demo 4: Custom YAML+SQL Loader — extending ProjectLoader.

Shows how consumers subclass ProjectLoader to support their own
file formats. This example loads YAML asset definitions with
companion SQL files.

Run: pip install pyyaml && python demos/04_custom_loader.py
"""

import shutil
import tempfile
from pathlib import Path
from typing import Any

import yaml

from assets import (
    Asset,
    AssetField,
    Environment,
    EnvironmentConfig,
    MemoryBackend,
    ProjectLoader,
    Registry,
    StateManager,
)

# ──────────────────────────────────────────────────────────────
# 1. Define asset types
# ──────────────────────────────────────────────────────────────

class Column(Asset):
    type: str = ""
    description: str = ""
    pii: bool = False


class DataModel(Asset):
    columns: list[Column] = AssetField(default_factory=list, field_source=True)


# ──────────────────────────────────────────────────────────────
# 2. Custom YAML+SQL loader
# ──────────────────────────────────────────────────────────────

class YAMLSQLLoader(ProjectLoader):
    """Loads YAML definitions with optional companion .sql files.

    File layout:
        models/
        ├── raw/
        │   └── users.yaml
        └── staging/
            ├── users.yaml
            └── users.sql     <- SQL in separate file
    """

    def discover_files(self, project_dir: Path) -> list[Path]:
        """Find all .yaml and .yml files (skip .sql — those are companions)."""
        return sorted(
            p for p in project_dir.rglob("*")
            if p.suffix in (".yaml", ".yml")
        )

    def parse_file(self, path: Path, root: Path) -> dict[str, Any] | None:
        """Parse YAML + merge companion SQL if present."""
        if path.suffix not in (".yaml", ".yml"):
            return None

        data = yaml.safe_load(path.read_text())
        if not isinstance(data, dict):
            return None

        # Look for companion .sql file
        sql_path = path.with_suffix(".sql")
        if sql_path.exists():
            data["sql"] = sql_path.read_text()

        return data


# ──────────────────────────────────────────────────────────────
# 3. Create a sample project with YAML+SQL files
# ──────────────────────────────────────────────────────────────

tmpdir = tempfile.mkdtemp(prefix="assets_yaml_demo_")
models_dir = Path(tmpdir) / "models"
(models_dir / "raw").mkdir(parents=True)
(models_dir / "staging").mkdir(parents=True)
(models_dir / "mart").mkdir(parents=True)

print(f"Project directory: {tmpdir}\n")

# raw/users.yaml (no SQL needed for sources)
(models_dir / "raw" / "users.yaml").write_text(yaml.dump({
    "name": "raw.users",
    "kind": "source",
    "tags": ["raw", "pii"],
    "columns": [
        {"name": "user_id", "type": "INTEGER"},
        {"name": "email", "type": "VARCHAR", "pii": True},
        {"name": "created_at", "type": "TIMESTAMP"},
    ],
}))

# raw/products.yaml
(models_dir / "raw" / "products.yaml").write_text(yaml.dump({
    "name": "raw.products",
    "kind": "source",
    "tags": ["raw"],
    "columns": [
        {"name": "product_id", "type": "INTEGER"},
        {"name": "name", "type": "VARCHAR"},
        {"name": "price", "type": "DECIMAL"},
    ],
}))

# staging/users.yaml (YAML metadata)
(models_dir / "staging" / "users.yaml").write_text(yaml.dump({
    "name": "staging.users",
    "kind": "data_model",
    "description": "Cleaned and validated user data",
    "tags": ["staging", "pii"],
    "columns": [
        {"name": "user_id", "type": "INTEGER"},
        {"name": "email_clean", "type": "VARCHAR", "pii": True,
         "description": "Lowercased, trimmed email"},
        {"name": "created_at", "type": "TIMESTAMP"},
    ],
}))

# staging/users.sql (companion SQL file — merged automatically)
(models_dir / "staging" / "users.sql").write_text("""\
SELECT
    u.user_id,
    LOWER(TRIM(u.email)) AS email_clean,
    u.created_at
FROM {{ ref('raw.users') }} u
WHERE u.email IS NOT NULL
""")

# mart/catalog.yaml + mart/catalog.sql
(models_dir / "mart" / "catalog.yaml").write_text(yaml.dump({
    "name": "mart.catalog",
    "kind": "data_model",
    "tags": ["mart"],
    "columns": [
        {"name": "product_id", "type": "INTEGER"},
        {"name": "name", "type": "VARCHAR"},
        {"name": "price", "type": "DECIMAL"},
    ],
}))

(models_dir / "mart" / "catalog.sql").write_text("""\
SELECT
    p.product_id,
    p.name,
    p.price
FROM {{ ref('raw.products') }} p
WHERE p.price > 0
""")

# ──────────────────────────────────────────────────────────────
# 4. Load project with custom loader
# ──────────────────────────────────────────────────────────────

print("=== Loading YAML+SQL Project ===\n")

registry = Registry()
loader = YAMLSQLLoader(
    registry,
    asset_class=DataModel,
    cache_dir=str(Path(tmpdir) / ".cache"),
)

result = loader.load(str(models_dir))
print(f"Loaded: {result.loaded} assets (reused={result.reused}, recompiled={result.recompiled})")
if result.errors:
    for err in result.errors:
        print(f"  Error in {err.path}: {err.error}")

# ──────────────────────────────────────────────────────────────
# 5. Verify everything loaded correctly
# ──────────────────────────────────────────────────────────────

print("\n=== Registered Assets ===\n")

for asset in registry.all():
    if "/" in asset.name:
        continue  # skip children
    deps = f" -> depends_on: {asset.depends_on}" if asset.depends_on else ""
    sql_info = " (has SQL)" if asset.sql else ""
    n_children = len(registry.children(asset.name))
    children_info = f" ({n_children} fields)" if n_children else ""
    print(f"  {asset.name} [kind={asset.kind}]{sql_info}{children_info}{deps}")

# ──────────────────────────────────────────────────────────────
# 6. Graph queries
# ──────────────────────────────────────────────────────────────

print("\n=== Graph ===\n")

graph = registry.graph
print(f"Topological order: {graph.topological_sort()}")
print(f"Roots: {graph.roots()}")
print(f"Leaves: {graph.leaves()}")

# Check that SQL was merged from companion files
staging_users = registry.get("staging.users")
print(f"\nstaging.users SQL loaded: {'LOWER(TRIM' in staging_users.sql}")
print(f"staging.users fields: {staging_users.list_fields()}")

# ──────────────────────────────────────────────────────────────
# 7. Plan/Apply with the custom loader
# ──────────────────────────────────────────────────────────────

print("\n=== Plan/Apply ===\n")

backend = MemoryBackend()
config = EnvironmentConfig(
    default="production",
    environments={"production": Environment(name="production")},
)
manager = StateManager(registry, loader, backend, config)

plan = manager.plan(str(models_dir), environment="production")
print(plan.show())

result = manager.apply(plan, environment="production")
print(f"\nApplied: created={result.created}")

# Second load — should hit compiled cache
print("\n=== Second Load (cached) ===\n")
registry.clear()
result2 = loader.load(str(models_dir))
print(f"Loaded: {result2.loaded} (reused={result2.reused}, recompiled={result2.recompiled})")

shutil.rmtree(tmpdir)
print(f"\nCleaned up {tmpdir}")
