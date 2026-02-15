#!/usr/bin/env python3
"""Demo 2: Plan/Apply Workflow — Terraform-style change detection.

This demo creates a temporary project directory with JSON asset files,
then walks through the plan -> apply -> modify -> re-plan cycle.

Run: python demos/02_plan_apply_workflow/main.py
"""

import json
import shutil
import tempfile
from pathlib import Path

from assets import (
    Asset,
    Project,
    Environment,
    EnvironmentConfig,
    SQLiteBackend,
)

# ──────────────────────────────────────────────────────────────
# 1. Define asset types
# ──────────────────────────────────────────────────────────────


class DataModel(Asset):
    pass


# ──────────────────────────────────────────────────────────────
# 2. Helper: consumer-driven JSON loading
# ──────────────────────────────────────────────────────────────


def load_json_models(project: Project, models_dir: Path) -> None:
    """Discover, parse, and register JSON asset files."""
    for path in sorted(models_dir.rglob("*.json")):
        data = json.loads(path.read_text())
        asset = DataModel.model_validate(data)
        project.register(asset)


# ──────────────────────────────────────────────────────────────
# 3. Create a temporary project with asset files
# ──────────────────────────────────────────────────────────────

tmpdir = tempfile.mkdtemp(prefix="assets_demo_")
models_dir = Path(tmpdir) / "models"
models_dir.mkdir()

print(f"Project directory: {tmpdir}\n")

# Create initial asset files
(models_dir / "raw_users.json").write_text(
    json.dumps(
        {
            "id": "raw.users",
            "type": "source",
            "tags": ["raw"],
            "children": [
                {"id": "user_id", "type": "column"},
                {"id": "email", "type": "column"},
            ],
        }
    )
)

(models_dir / "raw_orders.json").write_text(
    json.dumps(
        {
            "id": "raw.orders",
            "type": "source",
            "tags": ["raw"],
            "children": [
                {"id": "order_id", "type": "column"},
                {"id": "user_id", "type": "column"},
                {"id": "total", "type": "column"},
            ],
        }
    )
)

(models_dir / "staging_users.json").write_text(
    json.dumps(
        {
            "id": "staging.users",
            "type": "data_model",
            "tags": ["staging"],
            "sql": "SELECT * FROM raw.users",
            "depends_on": ["raw.users"],
            "children": [
                {"id": "user_id", "type": "column"},
                {"id": "email", "type": "column"},
            ],
        }
    )
)

# ──────────────────────────────────────────────────────────────
# 4. Set up the state manager
# ──────────────────────────────────────────────────────────────

backend = SQLiteBackend.memory()
config = EnvironmentConfig(
    default="production",
    environments={"production": Environment(name="production")},
)
project = Project(
    environment="production",
    backend=backend,
    env_config=config,
)

# ──────────────────────────────────────────────────────────────
# 5. First plan — everything is new
# ──────────────────────────────────────────────────────────────

print("=== First Plan (initial) ===\n")
project.clear()
load_json_models(project, models_dir)
plan = project.plan()
print(plan.show())

# ──────────────────────────────────────────────────────────────
# 6. Apply the plan
# ──────────────────────────────────────────────────────────────

print("\n=== Apply ===\n")
result = project.apply(plan)
print(
    f"Applied: {result.applied} "
    f"(created={result.created}, updated={result.updated}, deleted={result.deleted})"
)

# ──────────────────────────────────────────────────────────────
# 7. Plan again — should show no changes
# ──────────────────────────────────────────────────────────────

print("\n=== Second Plan (no changes) ===\n")
project.clear()
load_json_models(project, models_dir)
plan2 = project.plan()
print(plan2.show())

# ──────────────────────────────────────────────────────────────
# 8. Make changes — update a file, add a new one, delete one
# ──────────────────────────────────────────────────────────────

print("\n=== Making Changes ===\n")

# Update: change staging.users description
print("  - Updating staging.users (adding description)")
(models_dir / "staging_users.json").write_text(
    json.dumps(
        {
            "id": "staging.users",
            "type": "data_model",
            "description": "Cleaned user data from raw source",
            "tags": ["staging"],
            "sql": "SELECT * FROM raw.users",
            "depends_on": ["raw.users"],
            "children": [
                {"id": "user_id", "type": "column"},
                {"id": "email", "type": "column"},
            ],
        }
    )
)

# Create: add a new mart model
print("  - Creating mart.user_orders")
(models_dir / "mart_user_orders.json").write_text(
    json.dumps(
        {
            "id": "mart.user_orders",
            "type": "data_model",
            "tags": ["mart"],
            "sql": (
                "SELECT u.*, o.total "
                "FROM staging.users u "
                "JOIN raw.orders o ON u.user_id = o.user_id"
            ),
            "depends_on": ["staging.users", "raw.orders"],
            "children": [
                {"id": "user_id", "type": "column"},
                {"id": "email", "type": "column"},
                {"id": "total", "type": "column"},
            ],
        }
    )
)

# Delete: remove raw.orders
print("  - Deleting raw.orders")
(models_dir / "raw_orders.json").unlink()

# ──────────────────────────────────────────────────────────────
# 9. Plan after changes — should detect all three types
# ──────────────────────────────────────────────────────────────

print("\n=== Third Plan (after changes) ===\n")
project.clear()
load_json_models(project, models_dir)
plan3 = project.plan()
print(plan3.show())

# ──────────────────────────────────────────────────────────────
# 10. Apply changes
# ──────────────────────────────────────────────────────────────

print("\n=== Apply Changes ===\n")
result3 = project.apply(plan3)
print(
    f"Applied: {result3.applied} "
    f"(created={result3.created}, updated={result3.updated}, deleted={result3.deleted})"
)

# ──────────────────────────────────────────────────────────────
# 11. Final plan — clean state
# ──────────────────────────────────────────────────────────────

print("\n=== Final Plan (clean) ===\n")
project.clear()
load_json_models(project, models_dir)
plan4 = project.plan()
print(plan4.show())

# Cleanup
shutil.rmtree(tmpdir)
print(f"\nCleaned up {tmpdir}")
