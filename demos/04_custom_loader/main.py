#!/usr/bin/env python3
"""Demo 4: Consumer-Driven YAML+SQL Loading with FileDiscovery.load().

Shows how consumers own file parsing via the ``Loader`` protocol, while
the library handles discovery, index diffing, state rehydration, and
registration through :meth:`FileDiscovery.load`.

The consumer-provided ``YamlSqlLoader`` handles:
- YAML parsing with companion SQL file discovery
- ``{{ ref() }}`` template resolution in SQL
- Dependency declaration for companion files

Run: pip install pyyaml && python demos/04_custom_loader/main.py
"""

import re
import shutil
import tempfile
from pathlib import Path
from typing import Any

import yaml

from assets import (
    Asset,
    Assets,
    LoadedAsset,
    SourceGroup,
)

# ──────────────────────────────────────────────────────────────
# 1. Define asset types
# ──────────────────────────────────────────────────────────────


class DataModel(Asset):
    pass


# ──────────────────────────────────────────────────────────────
# 2. Consumer-owned loader
# ──────────────────────────────────────────────────────────────

REF_PATTERN = re.compile(r"\{\{\s*ref\(\s*['\"]([^'\"]+)['\"]\s*\)\s*\}\}")


def resolve_refs(sql: str) -> tuple[str, list[str]]:
    """Resolve {{ ref('x') }} templates in SQL.

    Returns (clean_sql, depends_on_list).
    Consumer owns this — the library never parses SQL.
    """
    refs = list(dict.fromkeys(REF_PATTERN.findall(sql)))
    clean = REF_PATTERN.sub(lambda m: m.group(1), sql)
    return clean, refs


class YamlSqlLoader:
    """Parses YAML model files with optional companion SQL.

    Implements the ``Loader`` protocol:
    - Reads YAML metadata from the discovered file
    - Looks for a companion ``.sql`` file alongside it
    - Resolves ``{{ ref() }}`` templates in the SQL
    - Declares the companion SQL as a file dependency
    """

    def load(self, path: Path, root: Path) -> list[LoadedAsset]:
        """Parse a YAML+SQL model and return a LoadedAsset."""
        data = self._parse_yaml_model(path)
        if not data:
            return []

        asset = DataModel.model_validate(data)

        # Track companion SQL as a file dependency
        deps: list[tuple[Path, str]] = []
        sql_path = path.with_suffix(".sql")
        if sql_path.exists():
            deps.append((sql_path, "sql"))

        return [LoadedAsset(asset=asset, deps=deps)]

    def _parse_yaml_model(self, path: Path) -> dict[str, Any]:
        """Parse a YAML model file with optional companion SQL."""
        data = yaml.safe_load(path.read_text())
        if not isinstance(data, dict):
            return {}

        sql_path = path.with_suffix(".sql")
        if sql_path.exists():
            raw_sql = sql_path.read_text()
            clean_sql, refs = resolve_refs(raw_sql)
            data["sql"] = clean_sql
            if refs:
                data["depends_on"] = refs

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
(models_dir / "raw" / "users.yaml").write_text(
    yaml.dump(
        {
            "id": "raw.users",
            "type": "source",
            "tags": ["raw", "pii"],
            "children": [
                {"id": "user_id", "type": "column"},
                {"id": "email", "type": "column"},
                {"id": "created_at", "type": "column"},
            ],
        }
    )
)

# raw/products.yaml
(models_dir / "raw" / "products.yaml").write_text(
    yaml.dump(
        {
            "id": "raw.products",
            "type": "source",
            "tags": ["raw"],
            "children": [
                {"id": "product_id", "type": "column"},
                {"id": "name", "type": "column"},
                {"id": "price", "type": "column"},
            ],
        }
    )
)

# staging/users.yaml (YAML metadata)
(models_dir / "staging" / "users.yaml").write_text(
    yaml.dump(
        {
            "id": "staging.users",
            "type": "data_model",
            "description": "Cleaned and validated user data",
            "tags": ["staging", "pii"],
            "children": [
                {"id": "user_id", "type": "column"},
                {
                    "id": "email_clean",
                    "type": "column",
                    "description": "Lowercased, trimmed email",
                },
                {"id": "created_at", "type": "column"},
            ],
        }
    )
)

# staging/users.sql (companion SQL file — merged automatically)
# Uses {{ ref() }} templates that the consumer resolves before registering
(models_dir / "staging" / "users.sql").write_text("""\
SELECT
    u.user_id,
    LOWER(TRIM(u.email)) AS email_clean,
    u.created_at
FROM {{ ref('raw.users') }} u
WHERE u.email IS NOT NULL
""")

# mart/catalog.yaml + mart/catalog.sql
(models_dir / "mart" / "catalog.yaml").write_text(
    yaml.dump(
        {
            "id": "mart.catalog",
            "type": "data_model",
            "tags": ["mart"],
            "children": [
                {"id": "product_id", "type": "column"},
                {"id": "name", "type": "column"},
                {"id": "price", "type": "column"},
            ],
        }
    )
)

(models_dir / "mart" / "catalog.sql").write_text("""\
SELECT
    p.product_id,
    p.name,
    p.price
FROM {{ ref('raw.products') }} p
WHERE p.price > 0
""")

# ──────────────────────────────────────────────────────────────
# 4. Load project with FileDiscovery.load()
# ──────────────────────────────────────────────────────────────

print("=== Loading YAML+SQL Project ===\n")

local_path = Path(tmpdir) / ".state"
project = Assets(environment="production", state_dir=str(local_path))

groups = [
    SourceGroup(
        name="models",
        directory=models_dir,
        patterns=["*.yaml"],
        loader=YamlSqlLoader(),
    ),
]

result = project.load(groups)
print(
    f"Loaded: {result.loaded} assets "
    f"(from_state={result.from_state}, parsed={result.parsed})"
)

# ──────────────────────────────────────────────────────────────
# 5. Verify everything loaded correctly
# ──────────────────────────────────────────────────────────────

print("\n=== Registered Assets ===\n")

for asset in project.all():
    deps = f" -> depends_on: {asset.depends_on}" if asset.depends_on else ""
    sql_info = " (has SQL)" if asset.sql else ""
    print(f"  {asset.id} [type={asset.type}]{sql_info}{deps}")

# ──────────────────────────────────────────────────────────────
# 6. Graph queries
# ──────────────────────────────────────────────────────────────

print("\n=== Graph ===\n")

graph = project.graph
print(f"Topological order: {graph.topological_sort()}")
print(f"Roots: {graph.roots()}")
print(f"Leaves: {graph.leaves()}")

# Check that SQL was merged from companion files
staging_users = project.get("staging.users")
print(f"\nstaging.users SQL loaded: {'LOWER(TRIM' in staging_users.sql}")
print(f"staging.users children: {staging_users.list_children()}")

# ──────────────────────────────────────────────────────────────
# 7. Plan/Apply — persists to SQLite state
# ──────────────────────────────────────────────────────────────

print("\n=== Plan/Apply ===\n")

plan = project.plan()
print(plan.show())

apply_result = project.apply(plan)
print(f"\nApplied: created={apply_result.created}")

# ──────────────────────────────────────────────────────────────
# 8. Second load — fresh assets loaded from state backend
# ──────────────────────────────────────────────────────────────

print("\n=== Second Load (from state) ===\n")
project.clear()
result2 = project.load(groups)
print(
    f"Loaded: {result2.loaded} "
    f"(from_state={result2.from_state}, parsed={result2.parsed})"
)

# ──────────────────────────────────────────────────────────────
# 9. Staleness cascade via graph
# ──────────────────────────────────────────────────────────────

print("\n=== Staleness Cascade ===\n")
changed_names = {"raw.users"}
all_stale = graph.stale(changed_names)
print(f"Changed: {changed_names}")
print(f"All stale (incl. downstream): {all_stale}")

shutil.rmtree(tmpdir)
print(f"\nCleaned up {tmpdir}")
