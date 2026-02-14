"""VPC network resource declaration."""

from __future__ import annotations

from platform_defaults import COMMON_LABELS, PROJECT_ID
from resource_models import Network

network_shared_vpc = Network(
    id="shared-vpc",
    project_id=PROJECT_ID,
    labels=COMMON_LABELS,
    auto_create_subnetworks=False,
    routing_mode="GLOBAL",
)
