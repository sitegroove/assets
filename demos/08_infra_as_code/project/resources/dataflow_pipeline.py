"""Dataflow pipeline declaration."""

from __future__ import annotations

from platform_defaults import COMMON_LABELS, PROJECT_ID, REGION
from resource_models import DataflowJob

from resources.pubsub_topic import pubsub_topic_orders_ingest
from resources.service_account import sa_pipeline_runner
from resources.storage_bucket import storage_bucket_raw_events
from resources.subnet import subnet_analytics_us_central1

data_flow_job_orders_normalizer = DataflowJob(
    id="orders-normalizer",
    project_id=PROJECT_ID,
    location=REGION,
    labels=COMMON_LABELS,
    depends_on=[
        subnet_analytics_us_central1.id,
        pubsub_topic_orders_ingest.id,
        storage_bucket_raw_events.id,
        sa_pipeline_runner.id,
        "central-logs",  # cross-root dep on shared infra logging
    ],
    template="gs://dataflow-templates/latest/PubSub_to_BigQuery",
    worker_machine_type="e2-standard-2",
    max_workers=10,
)
