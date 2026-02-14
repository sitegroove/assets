"""Cloud Run service declaration."""

from __future__ import annotations

from platform_defaults import COMMON_LABELS, PROJECT_ID, REGION
from resource_models import CloudRunService

from resources.bigquery_dataset import bq_analytics
from resources.service_account import sa_pipeline_runner

cloud_run_analytics_api = CloudRunService(
    id="analytics-api",
    project_id=PROJECT_ID,
    location=REGION,
    labels=COMMON_LABELS,
    depends_on=[bq_analytics.id, sa_pipeline_runner.id],
    service_name="analytics-api",
    image="us-docker.pkg.dev/acme/analytics/api:demo",
    min_instances=1,
    max_instances=20,
)
