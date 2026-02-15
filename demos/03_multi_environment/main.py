#!/usr/bin/env python3
"""Demo 3: Multi-Environment Workflow — shallow dev envs, promotion.

Shows how shallow environments inherit from parent, store only overrides,
and how promotion moves changes between environments.

Run: python demos/03_multi_environment/main.py
"""

import json
import shutil
import tempfile
from pathlib import Path

from assets import (
    Asset,
    Assets,
    Environment,
    EnvironmentConfig,
    SQLiteBackend,
)


class DataModel(Asset):
    pass


# ──────────────────────────────────────────────────────────────
# Helper: consumer-driven loading
# ──────────────────────────────────────────────────────────────


def load_json_models(project: Assets, models_dir: Path) -> None:
    """Discover, parse, and register JSON asset files."""
    for path in sorted(models_dir.rglob("*.json")):
        data = json.loads(path.read_text())
        asset = DataModel.model_validate(data)
        project.register(asset)


# ──────────────────────────────────────────────────────────────
# 1. Set up project
# ──────────────────────────────────────────────────────────────

tmpdir = tempfile.mkdtemp(prefix="assets_multi_env_")
models_dir = Path(tmpdir) / "models"
models_dir.mkdir()

(models_dir / "users.json").write_text(
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
(models_dir / "orders.json").write_text(
    json.dumps(
        {
            "id": "raw.orders",
            "type": "source",
            "tags": ["raw"],
            "children": [
                {"id": "order_id", "type": "column"},
                {"id": "amount", "type": "column"},
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
# 2. Configure environments
# ──────────────────────────────────────────────────────────────

print("=== Environment Configuration ===\n")

config = EnvironmentConfig(
    default="development",
    environments={
        "production": Environment(name="production"),
        "staging": Environment(name="staging", parent="production"),
    },
)

backend = SQLiteBackend.memory()
project = Assets(
    environment="production",
    backend=backend,
    env_config=config,
    protected_environments={"production", "staging"},
)
manager = project.manager
assert manager is not None

print("Environments:")
for name, env in config.environments.items():
    print(f"  {name}: parent={env.parent}, shallow={env.shallow}")

# ──────────────────────────────────────────────────────────────
# 3. Apply to production
# ──────────────────────────────────────────────────────────────

print("\n=== Apply to Production ===\n")

project.clear()
load_json_models(project, models_dir)
plan = project.plan()
print(plan.show())
result = project.apply(plan)
print(f"\nApplied to production: {result.created} created")

# ──────────────────────────────────────────────────────────────
# 4. Create a shallow dev environment
# ──────────────────────────────────────────────────────────────

print("\n=== Create Dev Environment (shallow) ===\n")

dev_env = manager.create_environment("dev_alice", parent="production", shallow=True)
print(
    f"Created: name={dev_env.name}, parent={dev_env.parent}, shallow={dev_env.shallow}"
)

# Dev inherits everything from production — plan should show no changes
project.clear()
load_json_models(project, models_dir)
dev_plan = manager.plan(environment="dev_alice")
print(f"Plan for dev_alice: has_changes={dev_plan.has_changes}")

# ──────────────────────────────────────────────────────────────
# 5. Make a change in dev (modify a file, re-plan, apply)
# ──────────────────────────────────────────────────────────────

print("\n=== Dev: Modify staging.users ===\n")

(models_dir / "staging_users.json").write_text(
    json.dumps(
        {
            "id": "staging.users",
            "type": "data_model",
            "description": "Alice's improved staging users",
            "tags": ["staging", "improved"],
            "sql": "SELECT * FROM raw.users WHERE email IS NOT NULL",
            "depends_on": ["raw.users"],
            "children": [
                {"id": "user_id", "type": "column"},
                {"id": "email", "type": "column"},
                {"id": "is_valid", "type": "column"},
            ],
        }
    )
)

project.clear()
load_json_models(project, models_dir)
dev_plan2 = manager.plan(environment="dev_alice")
print(dev_plan2.show())

dev_result = manager.apply(dev_plan2, environment="dev_alice")
print(f"\nApplied to dev_alice: updated={dev_result.updated}")

# ──────────────────────────────────────────────────────────────
# 6. Production is still clean (unaffected by dev changes)
# ──────────────────────────────────────────────────────────────

print("\n=== Production Still Clean ===\n")

# Restore the original file for production plan
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

project.clear()
load_json_models(project, models_dir)
prod_plan = manager.plan(environment="production")
print(f"Production plan has_changes: {prod_plan.has_changes}")

# ──────────────────────────────────────────────────────────────
# 7. Promote dev_alice -> staging
# ──────────────────────────────────────────────────────────────

print("\n=== Promote dev_alice -> staging ===\n")

promote_plan = manager.promote_to("staging", from_env="dev_alice")
print(promote_plan.show())

if promote_plan.has_changes:
    promote_result = manager.apply(promote_plan, environment="staging")
    print(f"\nPromoted to staging: {promote_result.applied} change(s)")

# ──────────────────────────────────────────────────────────────
# 8. Promote staging -> production
# ──────────────────────────────────────────────────────────────

print("\n=== Promote staging -> production ===\n")

promote_plan2 = manager.promote_to("production", from_env="staging")
print(promote_plan2.show())

if promote_plan2.has_changes:
    promote_result2 = manager.apply(promote_plan2, environment="production")
    print(f"\nPromoted to production: {promote_result2.applied} change(s)")

# ──────────────────────────────────────────────────────────────
# 9. Cleanup dev environment
# ──────────────────────────────────────────────────────────────

print("\n=== Cleanup ===\n")

manager.destroy_environment("dev_alice")
print("Destroyed dev_alice environment")

print(f"Remaining environments: {backend.list_environments()}")

# Protected environments can't be destroyed
try:
    manager.destroy_environment("production")
except ValueError as e:
    print(f"Cannot destroy production: {e}")

shutil.rmtree(tmpdir)
print(f"\nCleaned up {tmpdir}")
