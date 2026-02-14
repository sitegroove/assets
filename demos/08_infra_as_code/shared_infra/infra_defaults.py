"""Defaults for the shared infrastructure module.

This module lives at a different filesystem root than the main
project, simulating a vendored or separately-installed package.
"""

PROJECT_ID = "acme-shared-infra"
REGION = "us-central1"

INFRA_LABELS = {
    "owner": "platform-sre",
    "cost_center": "infrastructure",
    "managed_by": "shared-infra-module",
}
