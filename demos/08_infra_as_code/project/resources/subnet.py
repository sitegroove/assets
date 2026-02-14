"""Subnet resource declaration."""

from __future__ import annotations

from platform_defaults import COMMON_LABELS, PROJECT_ID, REGION
from resource_models import Subnet

from resources.network import network_shared_vpc

subnet_analytics_us_central1 = Subnet(
    id="analytics-us-central1",
    project_id=PROJECT_ID,
    location=REGION,
    labels=COMMON_LABELS,
    depends_on=[network_shared_vpc.id],
    ip_cidr_range="10.42.0.0/20",
    private_ip_google_access=True,
)
