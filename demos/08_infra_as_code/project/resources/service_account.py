"""Service account declaration."""

from __future__ import annotations

from platform_defaults import COMMON_LABELS, PROJECT_ID
from resource_models import ServiceAccount

sa_pipeline_runner = ServiceAccount(
    id="pipeline-runner",
    project_id=PROJECT_ID,
    labels=COMMON_LABELS,
    account_id="pipeline-runner",
    display_name="Pipeline Runner",
    roles=[
        "roles/bigquery.dataEditor",
        "roles/storage.objectAdmin",
        "roles/pubsub.subscriber",
    ],
)
