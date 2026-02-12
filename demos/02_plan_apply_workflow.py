#!/usr/bin/env python3
"""Demo 2: Plan/Apply Workflow — Terraform-style change detection.

This demo creates a temporary project directory with JSON asset files,
then walks through the plan → apply → modify → re-plan cycle.

Run: python demos/02_plan_apply_workflow.py
"""

import json
import shutil
import tempfile
from pathlib import Path

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
    pii: bool = False


class DataModel(Asset):
    columns: list[Column] = AssetField(default_factory=list, field_source=True)


# ──────────────────────────────────────────────────────────────
# 2. Create a temporary project with asset files
# ──────────────────────────────────────────────────────────────

tmpdir = tempfile.mkdtemp(prefix="assets_demo_")
models_dir = Path(tmpdir) / "models"
models_dir.mkdir()

print(f"Project directory: {tmpdir}\n")

# Create initial asset files
(models_dir / "raw_users.json").write_text(json.dumps({
    "name": "raw.users",
    "kind": "source",
    "tags": ["raw"],
    "columns": [
        {"name": "user_id", "type": "INTEGER"},
        {"name": "email", "type": "VARCHAR", "pii": True},
    ],
}))

(models_dir / "raw_orders.json").write_text(json.dumps({
    "name": "raw.orders",
    "kind": "source",
    "tags": ["raw"],
    "columns": [
        {"name": "order_id", "type": "INTEGER"},
        {"name": "user_id", "type": "INTEGER"},
        {"name": "total", "type": "DECIMAL"},
    ],
}))

(models_dir / "staging_users.json").write_text(json.dumps({
    "name": "staging.users",
    "kind": "data_model",
    "tags": ["staging"],
    "sql": "SELECT * FROM {{ ref('raw.users') }}",
    "columns": [
        {"name": "user_id", "type": "INTEGER"},
        {"name": "email", "type": "VARCHAR"},
    ],
}))

# ──────────────────────────────────────────────────────────────
# 3. Set up the state manager
# ──────────────────────────────────────────────────────────────

registry = Registry()
loader = ProjectLoader(
    registry,
    asset_class=DataModel,
    cache_dir=str(Path(tmpdir) / ".cache"),
)
backend = MemoryBackend()
config = EnvironmentConfig(
    default="production",
    environments={"production": Environment(name="production")},
)
manager = StateManager(registry, loader, backend, config)

# ──────────────────────────────────────────────────────────────
# 4. First plan — everything is new
# ──────────────────────────────────────────────────────────────

print("=== First Plan (initial) ===\n")
plan = manager.plan(str(models_dir), environment="production")
print(plan.show())

# ──────────────────────────────────────────────────────────────
# 5. Apply the plan
# ──────────────────────────────────────────────────────────────

print("\n=== Apply ===\n")
result = manager.apply(plan, environment="production")
print(f"Applied: {result.applied} "
      f"(created={result.created}, updated={result.updated}, deleted={result.deleted})")

# ──────────────────────────────────────────────────────────────
# 6. Plan again — should show no changes
# ──────────────────────────────────────────────────────────────

print("\n=== Second Plan (no changes) ===\n")
plan2 = manager.plan(str(models_dir), environment="production")
print(plan2.show())

# ──────────────────────────────────────────────────────────────
# 7. Make changes — update a file, add a new one, delete one
# ──────────────────────────────────────────────────────────────

print("\n=== Making Changes ===\n")

# Update: change staging.users description
print("  - Updating staging.users (adding description)")
(models_dir / "staging_users.json").write_text(json.dumps({
    "name": "staging.users",
    "kind": "data_model",
    "description": "Cleaned user data from raw source",
    "tags": ["staging"],
    "sql": "SELECT * FROM {{ ref('raw.users') }}",
    "columns": [
        {"name": "user_id", "type": "INTEGER"},
        {"name": "email", "type": "VARCHAR"},
    ],
}))

# Create: add a new mart model
print("  - Creating mart.user_orders")
(models_dir / "mart_user_orders.json").write_text(json.dumps({
    "name": "mart.user_orders",
    "kind": "data_model",
    "tags": ["mart"],
    "sql": (
        "SELECT u.*, o.total "
        "FROM {{ ref('staging.users') }} u "
        "JOIN {{ ref('raw.orders') }} o ON u.user_id = o.user_id"
    ),
    "columns": [
        {"name": "user_id", "type": "INTEGER"},
        {"name": "email", "type": "VARCHAR"},
        {"name": "total", "type": "DECIMAL"},
    ],
}))

# Delete: remove raw.orders
print("  - Deleting raw.orders")
(models_dir / "raw_orders.json").unlink()

# ──────────────────────────────────────────────────────────────
# 8. Plan after changes — should detect all three types
# ──────────────────────────────────────────────────────────────

print("\n=== Third Plan (after changes) ===\n")
plan3 = manager.plan(str(models_dir), environment="production")
print(plan3.show())

# ──────────────────────────────────────────────────────────────
# 9. Apply changes
# ──────────────────────────────────────────────────────────────

print("\n=== Apply Changes ===\n")
result3 = manager.apply(plan3, environment="production")
print(f"Applied: {result3.applied} "
      f"(created={result3.created}, updated={result3.updated}, deleted={result3.deleted})")

# ──────────────────────────────────────────────────────────────
# 10. Final plan — clean state
# ──────────────────────────────────────────────────────────────

print("\n=== Final Plan (clean) ===\n")
plan4 = manager.plan(str(models_dir), environment="production")
print(plan4.show())

# Cleanup
shutil.rmtree(tmpdir)
print(f"\nCleaned up {tmpdir}")
