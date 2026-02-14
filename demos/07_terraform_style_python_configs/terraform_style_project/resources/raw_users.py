"""raw.users source asset (declarative config)."""

from __future__ import annotations

from project_models import Column, DataModel

ASSET = DataModel(
    id="raw.users",
    type="source",
    tags=["raw", "pii"],
    owner_team="data-platform",
    materialization="external",
    children=[
        Column(id="user_id", type="INTEGER"),
        Column(id="email", type="VARCHAR", pii=True),
        Column(id="country", type="VARCHAR"),
        Column(id="created_at", type="TIMESTAMP"),
    ],
)
