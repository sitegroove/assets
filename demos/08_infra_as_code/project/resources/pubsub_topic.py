"""Pub/Sub topic declaration."""

from __future__ import annotations

from platform_defaults import COMMON_LABELS, PROJECT_ID
from resource_models import PubSubTopic

pubsub_topic_orders_ingest = PubSubTopic(
    id="orders-ingest",
    project_id=PROJECT_ID,
    labels=COMMON_LABELS,
    topic_name="orders-ingest",
    message_retention_duration="604800s",
)
