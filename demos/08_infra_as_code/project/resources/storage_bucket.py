"""Cloud Storage bucket declaration."""

from __future__ import annotations

from platform_defaults import COMMON_LABELS, PROJECT_ID, REGION
from resource_models import StorageBucket

from resources.network import network_shared_vpc

storage_bucket_raw_events = StorageBucket(
    id="raw-events",
    project_id=PROJECT_ID,
    location=REGION,
    labels=COMMON_LABELS,
    depends_on=[network_shared_vpc.id],
    bucket_name="acme-raw-events",
    versioning=True,
    uniform_bucket_level_access=True,
)
