#!/usr/bin/env python3
"""Demo 3: Multi-Environment Workflow — shallow dev envs, promotion.

Shows how shallow environments inherit from parent, store only overrides,
and how promotion moves changes between environments.

Run: python demos/03_multi_environment.py
"""

import json
import shutil
import tempfile
from pathlib import Path

from assets import (
    Asset,
    Environment,
    EnvironmentConfig,
    MemoryBackend,
    ProjectLoader,
    Registry,
    StateManager,
)


class DataModel(Asset):
    pass


# ──────────────────────────────────────────────────────────────
# 1. Set up project
# ──────────────────────────────────────────────────────────────

tmpdir = tempfile.mkdtemp(prefix="assets_multi_env_")
models_dir = Path(tmpdir) / "models"
models_dir.mkdir()

(models_dir / "users.json").write_text(json.dumps({
    "name": "raw.users", "kind": "source", "tags": ["raw"],
    "children": [{"name": "user_id", "kind": "column"}, {"name": "email", "kind": "column"}],
}))
(models_dir / "orders.json").write_text(json.dumps({
    "name": "raw.orders", "kind": "source", "tags": ["raw"],
    "children": [{"name": "order_id", "kind": "column"}, {"name": "amount", "kind": "column"}],
}))
(models_dir / "staging_users.json").write_text(json.dumps({
    "name": "staging.users", "kind": "data_model", "tags": ["staging"],
    "sql": "SELECT * FROM {{ ref('raw.users') }}",
    "children": [{"name": "user_id", "kind": "column"}, {"name": "email", "kind": "column"}],
}))

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

backend = MemoryBackend()
registry = Registry()
loader = ProjectLoader(registry, asset_class=DataModel, cache_dir=str(Path(tmpdir) / ".cache"))
manager = StateManager(registry, loader, backend, config)

print("Environments:")
for name, env in config.environments.items():
    print(f"  {name}: parent={env.parent}, shallow={env.shallow}")

# ──────────────────────────────────────────────────────────────
# 3. Apply to production
# ──────────────────────────────────────────────────────────────

print("\n=== Apply to Production ===\n")

plan = manager.plan(str(models_dir), environment="production")
print(plan.show())
result = manager.apply(plan, environment="production")
print(f"\nApplied to production: {result.created} created")

# ──────────────────────────────────────────────────────────────
# 4. Create a shallow dev environment
# ──────────────────────────────────────────────────────────────

print("\n=== Create Dev Environment (shallow) ===\n")

dev_env = manager.create_environment("dev_alice", parent="production", shallow=True)
print(f"Created: name={dev_env.name}, parent={dev_env.parent}, shallow={dev_env.shallow}")

# Dev inherits everything from production — plan should show no changes
dev_plan = manager.plan(str(models_dir), environment="dev_alice")
print(f"Plan for dev_alice: has_changes={dev_plan.has_changes}")

# ──────────────────────────────────────────────────────────────
# 5. Make a change in dev (modify a file, re-plan, apply)
# ──────────────────────────────────────────────────────────────

print("\n=== Dev: Modify staging.users ===\n")

(models_dir / "staging_users.json").write_text(json.dumps({
    "name": "staging.users", "kind": "data_model",
    "description": "Alice's improved staging users",
    "tags": ["staging", "improved"],
    "sql": "SELECT * FROM {{ ref('raw.users') }} WHERE email IS NOT NULL",
    "children": [
        {"name": "user_id", "kind": "column"},
        {"name": "email", "kind": "column"},
        {"name": "is_valid", "kind": "column"},
    ],
}))

dev_plan2 = manager.plan(str(models_dir), environment="dev_alice")
print(dev_plan2.show())

dev_result = manager.apply(dev_plan2, environment="dev_alice")
print(f"\nApplied to dev_alice: updated={dev_result.updated}")

# ──────────────────────────────────────────────────────────────
# 6. Production is still clean (unaffected by dev changes)
# ──────────────────────────────────────────────────────────────

print("\n=== Production Still Clean ===\n")

# Restore the original file for production plan
(models_dir / "staging_users.json").write_text(json.dumps({
    "name": "staging.users", "kind": "data_model", "tags": ["staging"],
    "sql": "SELECT * FROM {{ ref('raw.users') }}",
    "children": [{"name": "user_id", "kind": "column"}, {"name": "email", "kind": "column"}],
}))

prod_plan = manager.plan(str(models_dir), environment="production")
print(f"Production plan has_changes: {prod_plan.has_changes}")

# ──────────────────────────────────────────────────────────────
# 7. Promote dev_alice -> staging
# ──────────────────────────────────────────────────────────────

print("\n=== Promote dev_alice -> staging ===\n")

promote_plan = manager.promote(from_env="dev_alice", to_env="staging")
print(promote_plan.show())

if promote_plan.has_changes:
    promote_result = manager.apply(promote_plan, environment="staging")
    print(f"\nPromoted to staging: {promote_result.applied} change(s)")

# ──────────────────────────────────────────────────────────────
# 8. Promote staging -> production
# ──────────────────────────────────────────────────────────────

print("\n=== Promote staging -> production ===\n")

promote_plan2 = manager.promote(from_env="staging", to_env="production")
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
