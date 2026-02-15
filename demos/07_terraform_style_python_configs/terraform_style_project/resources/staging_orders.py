"""order-service: order management service (declarative config)."""

from __future__ import annotations

from project_models import Service

ASSET = Service(
    id="order-service",
    type="backend",
    tags=["core", "commerce"],
    owner_team="team-commerce",
    language="go",
    port=8002,
    depends_on=["order-db", "user-service"],
)
