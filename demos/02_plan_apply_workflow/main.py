#!/usr/bin/env python3
"""Demo 2: Plan/Apply Workflow — Terraform-style change detection.

Uses **feature flags** as the domain.  The demo walks through the full
plan -> apply -> modify -> re-plan cycle without any SQL or data models.

What you will learn:
  - Setting up a Project with a state backend
  - Creating assets and running your first plan
  - Applying changes to persist state
  - Detecting creates, updates, and deletes across plan cycles

Run: python demos/02_plan_apply_workflow/main.py
"""

from assets import (
    Asset,
    AssetField,
    Environment,
    EnvironmentConfig,
    Project,
    SQLiteBackend,
)

# ──────────────────────────────────────────────────────────────
# 1. Define asset types
# ──────────────────────────────────────────────────────────────


class FeatureFlag(Asset):
    """A feature flag that controls runtime behavior.

    ``enabled`` and ``rollout_pct`` are part of the fingerprint, so
    toggling a flag or changing its rollout counts as a real change.
    ``description`` is also fingerprinted by default.
    """

    enabled: bool = False
    rollout_pct: int = 0  # 0-100
    description: str = ""


# ──────────────────────────────────────────────────────────────
# 2. Set up the project with an in-memory state backend
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
# 3. Register initial feature flags
# ──────────────────────────────────────────────────────────────


def register_initial_flags() -> None:
    """Register the initial set of feature flags."""
    project.register(
        FeatureFlag(
            id="dark-mode",
            type="ui",
            tags=["frontend", "experiment"],
            enabled=True,
            rollout_pct=50,
            description="Dark mode theme toggle",
        )
    )
    project.register(
        FeatureFlag(
            id="new-checkout",
            type="ui",
            tags=["frontend", "experiment"],
            enabled=False,
            rollout_pct=0,
            description="Redesigned checkout flow",
        )
    )
    project.register(
        FeatureFlag(
            id="rate-limiter",
            type="backend",
            tags=["infra", "security"],
            enabled=True,
            rollout_pct=100,
            description="API rate limiting",
        )
    )


# ──────────────────────────────────────────────────────────────
# 4. First plan — everything is new
# ──────────────────────────────────────────────────────────────

print("=== First Plan (initial) ===\n")
register_initial_flags()
plan = project.plan()
print(plan.show())

# ──────────────────────────────────────────────────────────────
# 5. Apply the plan
# ──────────────────────────────────────────────────────────────

print("\n=== Apply ===\n")
result = project.apply(plan)
print(
    f"Applied: {result.applied} "
    f"(created={result.created}, updated={result.updated}, "
    f"deleted={result.deleted})"
)

# ──────────────────────────────────────────────────────────────
# 6. Plan again — should show no changes
# ──────────────────────────────────────────────────────────────

print("\n=== Second Plan (no changes) ===\n")
project.clear()
register_initial_flags()
plan2 = project.plan()
print(plan2.show())

# ──────────────────────────────────────────────────────────────
# 7. Make changes — update a flag, add a new one, delete one
# ──────────────────────────────────────────────────────────────

print("\n=== Making Changes ===\n")

project.clear()

# Update: roll out new-checkout to 25%
print("  - Updating new-checkout (enabling, rollout=25)")
project.register(
    FeatureFlag(
        id="new-checkout",
        type="ui",
        tags=["frontend", "experiment"],
        enabled=True,
        rollout_pct=25,
        description="Redesigned checkout flow",
    )
)

# Keep dark-mode unchanged
project.register(
    FeatureFlag(
        id="dark-mode",
        type="ui",
        tags=["frontend", "experiment"],
        enabled=True,
        rollout_pct=50,
        description="Dark mode theme toggle",
    )
)

# Create: add a new flag
print("  - Creating beta-search")
project.register(
    FeatureFlag(
        id="beta-search",
        type="backend",
        tags=["search", "experiment"],
        enabled=False,
        rollout_pct=0,
        description="New search algorithm",
    )
)

# Delete: don't re-register rate-limiter (it was in state but not desired)
print("  - Removing rate-limiter (not re-registered)")

# ──────────────────────────────────────────────────────────────
# 8. Plan after changes — should detect all three types
# ──────────────────────────────────────────────────────────────

print("\n=== Third Plan (after changes) ===\n")
plan3 = project.plan()
print(plan3.show())

# ──────────────────────────────────────────────────────────────
# 9. Apply changes
# ──────────────────────────────────────────────────────────────

print("\n=== Apply Changes ===\n")
result3 = project.apply(plan3)
print(
    f"Applied: {result3.applied} "
    f"(created={result3.created}, updated={result3.updated}, "
    f"deleted={result3.deleted})"
)

# ──────────────────────────────────────────────────────────────
# 10. Final plan — clean state
# ──────────────────────────────────────────────────────────────

print("\n=== Final Plan (clean) ===\n")
plan4 = project.plan()
print(plan4.show())
