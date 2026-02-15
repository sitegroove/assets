"""user-service: core user management service (declarative config)."""

from __future__ import annotations

from project_models import Service

ASSET = Service(
    id="user-service",
    type="backend",
    tags=["core", "auth"],
    owner_team="team-identity",
    language="python",
    port=8001,
    depends_on=["user-db"],
)
