"""Resource models for shared infrastructure.

Re-uses GcpResource from the main project's resource_models but
adds infrastructure-specific resource types.
"""

from __future__ import annotations

from assets import Asset, AssetField


class InfraResource(Asset):
    """Base class for shared infrastructure assets."""

    project_id: str
    location: str = "global"
    labels: dict[str, str] = AssetField(default_factory=dict)


class LogBucket(InfraResource):
    """Cloud Logging bucket for centralized log storage."""

    type: str = "gcp_log_bucket"
    bucket_id: str
    retention_days: int = 30


class MonitoringAlertPolicy(InfraResource):
    """Cloud Monitoring alert policy."""

    type: str = "gcp_monitoring_alert_policy"
    policy_name: str
    conditions: list[str] = AssetField(default_factory=list)
    notification_channels: list[str] = AssetField(default_factory=list)
