"""web-frontend: the customer-facing web application (declarative config)."""

from __future__ import annotations

from project_models import Service

ASSET = Service(
    id="web-frontend",
    type="frontend",
    tags=["web"],
    owner_team="team-web",
    language="typescript",
    port=3000,
    depends_on=["user-service", "order-service"],
)
