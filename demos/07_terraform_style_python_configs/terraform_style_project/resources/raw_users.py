"""user-db: the primary user database (declarative config)."""

from __future__ import annotations

from project_models import Service

ASSET = Service(
    id="user-db",
    type="database",
    tags=["infra", "storage"],
    language="postgresql",
    owner_team="data-platform",
    port=5432,
)
