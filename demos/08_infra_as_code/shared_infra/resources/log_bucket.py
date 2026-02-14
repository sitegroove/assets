"""Centralized logging bucket declaration."""

from __future__ import annotations

from infra_defaults import INFRA_LABELS, PROJECT_ID, REGION
from infra_models import LogBucket

log_bucket_central = LogBucket(
    id="central-logs",
    project_id=PROJECT_ID,
    location=REGION,
    labels=INFRA_LABELS,
    bucket_id="acme-central-logs",
    retention_days=90,
)
