"""BigQuery dataset declaration."""

from __future__ import annotations

from platform_defaults import COMMON_LABELS, PROJECT_ID, REGION
from resource_models import BigQueryDataset

from resources.service_account import sa_pipeline_runner

bq_analytics = BigQueryDataset(
    id="analytics",
    project_id=PROJECT_ID,
    location=REGION,
    labels=COMMON_LABELS,
    depends_on=[sa_pipeline_runner.id],
    dataset_id="analytics",
    default_table_expiration_ms=2592000000,
)
