#!/usr/bin/env python3
"""Demo 6: ML Pipeline — rich domain modeling with plan/apply.

Models a machine-learning pipeline: datasets, trained models,
evaluations, and deployments.  Shows how the ``assets`` library
handles complex domain models with nested children, graph
traversal, selectors, and the full state lifecycle.

What you will learn:
  - Rich Pydantic model hierarchies (nested children, enums)
  - Using ``fingerprint=False`` for runtime-only metadata
  - Plan / apply / drift detection for ML artifacts
  - Impact analysis: "what retrains if this dataset changes?"
  - Selectors across a non-trivial graph

Run: python demos/06_ml_pipeline/main.py
"""

from __future__ import annotations

from typing import cast

from assets import (
    Asset,
    AssetField,
    Environment,
    EnvironmentConfig,
    Project,
    SQLiteBackend,
)

# ──────────────────────────────────────────────────────────────
# 1. Define domain models
# ──────────────────────────────────────────────────────────────


class Column(Asset):
    """A column in a dataset."""

    data_type: str = "string"
    nullable: bool = True
    pii: bool = False


class Dataset(Asset):
    """A dataset (table, file, feature store, etc.)."""

    format: str = "parquet"
    row_count: int = cast(int, AssetField(default=0, fingerprint=False))
    columns: list[Column] = cast(
        list[Column],
        AssetField(default_factory=list, children=True),
    )


class Hyperparameter(Asset):
    """A single hyperparameter for a model."""

    value: str = ""


class TrainedModel(Asset):
    """A trained ML model artifact."""

    framework: str = "sklearn"
    algorithm: str = ""
    hyperparameters: list[Hyperparameter] = cast(
        list[Hyperparameter],
        AssetField(default_factory=list, children=True),
    )
    accuracy: float = cast(float, AssetField(default=0.0, fingerprint=False))
    training_duration_s: int = cast(int, AssetField(default=0, fingerprint=False))


class Deployment(Asset):
    """A deployed model serving endpoint."""

    model_id: str = ""
    replicas: int = 1
    endpoint: str = ""


# ──────────────────────────────────────────────────────────────
# 2. Build the ML pipeline assets
# ──────────────────────────────────────────────────────────────


def register_pipeline(project: Project) -> None:
    """Register all assets in our ML pipeline."""

    # ── Datasets ──

    project.register(
        Dataset(
            id="raw.clickstream",
            type="dataset",
            tags=["raw", "events"],
            format="json",
            row_count=5_000_000,
            columns=[
                Column(id="event_id", data_type="string", nullable=False),
                Column(id="user_id", data_type="string", pii=True),
                Column(id="event_type", data_type="string"),
                Column(id="timestamp", data_type="datetime"),
                Column(id="page_url", data_type="string"),
            ],
        )
    )

    project.register(
        Dataset(
            id="raw.user_profiles",
            type="dataset",
            tags=["raw", "users"],
            row_count=200_000,
            columns=[
                Column(id="user_id", data_type="string", nullable=False, pii=True),
                Column(id="signup_date", data_type="date"),
                Column(id="country", data_type="string"),
                Column(id="plan_tier", data_type="string"),
            ],
        )
    )

    project.register(
        Dataset(
            id="features.user_activity",
            type="feature_table",
            tags=["features", "users"],
            depends_on=["raw.clickstream", "raw.user_profiles"],
            row_count=200_000,
            columns=[
                Column(id="user_id", data_type="string", nullable=False),
                Column(id="events_7d", data_type="integer"),
                Column(id="events_30d", data_type="integer"),
                Column(id="days_since_last_visit", data_type="integer"),
                Column(id="country", data_type="string"),
                Column(id="plan_tier", data_type="string"),
            ],
        )
    )

    project.register(
        Dataset(
            id="features.churn_labels",
            type="feature_table",
            tags=["features", "labels"],
            depends_on=["raw.clickstream"],
            row_count=200_000,
            columns=[
                Column(id="user_id", data_type="string", nullable=False),
                Column(id="churned", data_type="boolean"),
            ],
        )
    )

    # ── Trained models ──

    project.register(
        TrainedModel(
            id="model.churn_predictor",
            type="model",
            tags=["ml", "churn"],
            framework="sklearn",
            algorithm="gradient_boosting",
            depends_on=["features.user_activity", "features.churn_labels"],
            accuracy=0.87,
            training_duration_s=120,
            hyperparameters=[
                Hyperparameter(id="n_estimators", value="200"),
                Hyperparameter(id="max_depth", value="6"),
                Hyperparameter(id="learning_rate", value="0.1"),
            ],
        )
    )

    project.register(
        TrainedModel(
            id="model.user_segmentation",
            type="model",
            tags=["ml", "segmentation"],
            framework="sklearn",
            algorithm="kmeans",
            depends_on=["features.user_activity"],
            accuracy=0.72,
            training_duration_s=45,
            hyperparameters=[
                Hyperparameter(id="n_clusters", value="5"),
            ],
        )
    )

    # ── Deployments ──

    project.register(
        Deployment(
            id="deploy.churn_api",
            type="deployment",
            tags=["serving", "churn"],
            model_id="model.churn_predictor",
            depends_on=["model.churn_predictor"],
            replicas=3,
            endpoint="/predict/churn",
        )
    )

    project.register(
        Deployment(
            id="deploy.segmentation_batch",
            type="deployment",
            tags=["serving", "segmentation", "batch"],
            model_id="model.user_segmentation",
            depends_on=["model.user_segmentation"],
            replicas=1,
            endpoint="/batch/segmentation",
        )
    )


# ──────────────────────────────────────────────────────────────
# 3. Run the pipeline lifecycle
# ──────────────────────────────────────────────────────────────

backend = SQLiteBackend.memory()
env_config = EnvironmentConfig(
    default="production",
    environments={"production": Environment(name="production")},
)
project = Project(
    environment="production",
    backend=backend,
    env_config=env_config,
)
manager = project.manager
assert manager is not None

# ── Step 1: Register and plan ──

print("=" * 60)
print("  ML Pipeline Demo")
print("=" * 60)

register_pipeline(project)

print(f"\nRegistered {len(project)} assets:")
for a in project.all():
    n_kids = len(a.children())
    kids_str = f"  children={n_kids}" if n_kids else ""
    print(f"  {a.id:<30} type={a.type:<15}{kids_str}")

# ── Step 2: Graph exploration ──

print(f"\n{'─' * 60}")
print("  Graph exploration")
print(f"{'─' * 60}")

graph = project.graph
print(f"Roots (raw data):       {sorted(graph.roots())}")
print(f"Leaves (deployments):   {sorted(graph.leaves())}")
print(f"Topological order:      {graph.topological_sort()}")

# Impact analysis
impacted = graph.stale({"raw.clickstream"})
print(f"\nIf raw.clickstream changes, retrain: {sorted(impacted)}")

impacted2 = graph.stale({"features.user_activity"})
print(f"If features.user_activity changes:   {sorted(impacted2)}")

# ── Step 3: Selectors ──

print(f"\n{'─' * 60}")
print("  Selectors")
print(f"{'─' * 60}")

for sel in ["type:model", "tag:churn", "type:deployment", "+deploy.churn_api"]:
    result = project.select(sel)
    print(f"  {sel:<30} -> {sorted(result.names)}")

# ── Step 4: Nested asset introspection ──

print(f"\n{'─' * 60}")
print("  Nested asset introspection")
print(f"{'─' * 60}")

ds = project.get("features.user_activity")
print(f"features.user_activity columns: {[c.id for c in ds.children()]}")

churn = project.get("model.churn_predictor")
print(f"model.churn_predictor hyperparameters: {[h.id for h in churn.children()]}")

# PII scan
pii_fields = []
for asset in project.all():
    for child in asset.children():
        if getattr(child, "pii", False):
            pii_fields.append(f"{asset.id}/{child.id}")
print(f"PII columns: {pii_fields}")

# ── Step 5: Plan & apply ──

print(f"\n{'─' * 60}")
print("  Plan & apply")
print(f"{'─' * 60}")

plan = project.plan()
print(f"\n{plan.show()}")

apply_result = project.apply(plan)
print(
    f"Applied: created={apply_result.created}, "
    f"updated={apply_result.updated}, "
    f"deleted={apply_result.deleted}"
)

# Re-plan should be clean
project.clear()
register_pipeline(project)
plan2 = project.plan()
print(f"Re-plan has_changes: {plan2.has_changes}")

# ── Step 6: Simulate a model update (retrain with new hyperparameters) ──

print(f"\n{'─' * 60}")
print("  Retrain churn model (new hyperparameters)")
print(f"{'─' * 60}")

project.clear()
register_pipeline(project)

# Override the churn model with updated hyperparameters
project.unregister("model.churn_predictor")
project.register(
    TrainedModel(
        id="model.churn_predictor",
        type="model",
        tags=["ml", "churn"],
        framework="sklearn",
        algorithm="gradient_boosting",
        depends_on=["features.user_activity", "features.churn_labels"],
        accuracy=0.91,
        training_duration_s=180,
        hyperparameters=[
            Hyperparameter(id="n_estimators", value="500"),  # was 200
            Hyperparameter(id="max_depth", value="8"),  # was 6
            Hyperparameter(id="learning_rate", value="0.05"),  # was 0.1
        ],
    )
)

retrain_plan = project.plan()
print(f"\n{retrain_plan.show()}")

# ── Step 7: Drift detection ──

print(f"\n{'─' * 60}")
print("  Drift detection")
print(f"{'─' * 60}")

# Apply the retrain first
project.apply(retrain_plan)

# Now check drift (should be clean)
project.clear()
register_pipeline(project)
# Re-register the updated model
project.unregister("model.churn_predictor")
project.register(
    TrainedModel(
        id="model.churn_predictor",
        type="model",
        tags=["ml", "churn"],
        framework="sklearn",
        algorithm="gradient_boosting",
        depends_on=["features.user_activity", "features.churn_labels"],
        accuracy=0.91,
        training_duration_s=180,
        hyperparameters=[
            Hyperparameter(id="n_estimators", value="500"),
            Hyperparameter(id="max_depth", value="8"),
            Hyperparameter(id="learning_rate", value="0.05"),
        ],
    )
)

drift = project.drift()
print(f"Drift detected: {drift.has_changes}")

print(f"\n{'=' * 60}")
print("  Done!")
print(f"{'=' * 60}")
