#!/usr/bin/env python3
"""Demo 3: Multi-Environment Workflow — copy-on-create, promotion.

Uses **app configuration** as the domain: config entries like
timeouts, feature toggles, and limits are managed across environments
(production, staging, dev).

What you will learn:
  - Setting up multiple environments
  - Copy-on-create: new environments start as a snapshot of the parent
  - Making changes in a dev environment (isolated from production)
  - Promoting changes:  dev -> staging -> production
  - Destroying temporary environments
  - Protected environments

Run: python demos/03_multi_environment/main.py
"""

from assets import (
    Asset,
    Environment,
    EnvironmentConfig,
    Project,
    SQLiteBackend,
)

# ──────────────────────────────────────────────────────────────
# 1. Define asset type
# ──────────────────────────────────────────────────────────────


class ConfigEntry(Asset):
    """A single configuration entry (key/value with metadata)."""

    value: str = ""
    description: str = ""


# ──────────────────────────────────────────────────────────────
# 2. Helper: register the config entries we want
# ──────────────────────────────────────────────────────────────


def register_base_config(project: Project) -> None:
    """Register the baseline configuration entries."""
    project.register(
        ConfigEntry(
            id="api.timeout_ms",
            type="setting",
            tags=["api", "performance"],
            value="3000",
            description="HTTP request timeout in milliseconds",
        )
    )
    project.register(
        ConfigEntry(
            id="api.rate_limit",
            type="setting",
            tags=["api", "security"],
            value="100",
            description="Max requests per minute per user",
        )
    )
    project.register(
        ConfigEntry(
            id="feature.dark_mode",
            type="feature_flag",
            tags=["frontend", "experiment"],
            value="true",
            description="Enable dark mode theme",
        )
    )
    project.register(
        ConfigEntry(
            id="feature.new_search",
            type="feature_flag",
            tags=["search", "experiment"],
            value="false",
            description="New search algorithm",
        )
    )


# ──────────────────────────────────────────────────────────────
# 3. Configure environments
# ──────────────────────────────────────────────────────────────

print("=== Environment Configuration ===\n")

config = EnvironmentConfig(
    default="production",
    environments={
        "production": Environment(name="production"),
    },
)

backend = SQLiteBackend.memory()
project = Project(
    environment="production",
    backend=backend,
    env_config=config,
    protected_environments={"production", "staging"},
)
manager = project.manager
assert manager is not None

print("Environments:")
for name in config.environments:
    print(f"  {name}")

# ──────────────────────────────────────────────────────────────
# 4. Apply baseline to production
# ──────────────────────────────────────────────────────────────

print("\n=== Apply to Production ===\n")

register_base_config(project)
plan = project.plan()
print(plan.show())
result = project.apply(plan)
print(f"\nApplied to production: {result.created} created")

# ──────────────────────────────────────────────────────────────
# 5. Create dev environment (copy-on-create from production)
# ──────────────────────────────────────────────────────────────

print("\n=== Create Dev Environment (copy from production) ===\n")

dev_env = manager.create_environment("dev_alice", parent="production")
print(f"Created: name={dev_env.name}")

# Dev has a copy of production — plan should show no changes
project.clear()
register_base_config(project)
dev_plan = manager.plan(environment="dev_alice")
print(f"Plan for dev_alice: has_changes={dev_plan.has_changes}")

# ──────────────────────────────────────────────────────────────
# 6. Make a change in dev (modify config, re-plan, apply)
# ──────────────────────────────────────────────────────────────

print("\n=== Dev: Update Config ===\n")

project.clear()

# Keep existing entries but change two values
project.register(
    ConfigEntry(
        id="api.timeout_ms",
        type="setting",
        tags=["api", "performance"],
        value="5000",  # changed: 3000 -> 5000
        description="HTTP request timeout in milliseconds",
    )
)
project.register(
    ConfigEntry(
        id="api.rate_limit",
        type="setting",
        tags=["api", "security"],
        value="100",
        description="Max requests per minute per user",
    )
)
project.register(
    ConfigEntry(
        id="feature.dark_mode",
        type="feature_flag",
        tags=["frontend", "experiment"],
        value="true",
        description="Enable dark mode theme",
    )
)
project.register(
    ConfigEntry(
        id="feature.new_search",
        type="feature_flag",
        tags=["search", "experiment"],
        value="true",  # changed: false -> true
        description="New search algorithm (Alice's experiment)",
    )
)

dev_plan2 = manager.plan(environment="dev_alice")
print(dev_plan2.show())

dev_result = manager.apply(dev_plan2, environment="dev_alice")
print(f"\nApplied to dev_alice: updated={dev_result.updated}")

# ──────────────────────────────────────────────────────────────
# 7. Production is still clean (unaffected by dev changes)
# ──────────────────────────────────────────────────────────────

print("\n=== Production Still Clean ===\n")

project.clear()
register_base_config(project)
prod_plan = manager.plan(environment="production")
print(f"Production plan has_changes: {prod_plan.has_changes}")

# ──────────────────────────────────────────────────────────────
# 8. Promote dev_alice -> staging
# ──────────────────────────────────────────────────────────────

print("\n=== Promote dev_alice -> staging ===\n")

# Create staging from production first
manager.create_environment("staging", parent="production")

promote_plan = manager.promote_to("staging", from_env="dev_alice")
print(promote_plan.show())

if promote_plan.has_changes:
    promote_result = manager.apply(promote_plan, environment="staging")
    print(f"\nPromoted to staging: {promote_result.applied} change(s)")

# ──────────────────────────────────────────────────────────────
# 9. Promote staging -> production
# ──────────────────────────────────────────────────────────────

print("\n=== Promote staging -> production ===\n")

promote_plan2 = manager.promote_to("production", from_env="staging")
print(promote_plan2.show())

if promote_plan2.has_changes:
    promote_result2 = manager.apply(promote_plan2, environment="production")
    print(f"\nPromoted to production: {promote_result2.applied} change(s)")

# ──────────────────────────────────────────────────────────────
# 10. Cleanup
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
