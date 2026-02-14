"""Monitoring alert policy declaration."""

from __future__ import annotations

from infra_defaults import INFRA_LABELS, PROJECT_ID
from infra_models import MonitoringAlertPolicy

from resources.log_bucket import log_bucket_central

alert_policy_high_error_rate = MonitoringAlertPolicy(
    id="high-error-rate",
    project_id=PROJECT_ID,
    labels=INFRA_LABELS,
    depends_on=[log_bucket_central.id],
    policy_name="High Error Rate",
    conditions=["error_rate > 5%"],
    notification_channels=["pagerduty-oncall", "slack-sre"],
)
