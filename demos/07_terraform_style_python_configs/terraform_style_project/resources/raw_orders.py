"""raw.orders source asset (declarative config)."""

from __future__ import annotations

from project_models import Column, DataModel

ASSET = DataModel(
    id="raw.orders",
    type="source",
    tags=["raw", "finance"],
    owner_team="data-platform",
    materialization="external",
    children=[
        Column(id="order_id", type="INTEGER"),
        Column(id="user_id", type="INTEGER"),
        Column(id="amount", type="DECIMAL"),
        Column(id="status", type="VARCHAR"),
        Column(id="ordered_at", type="TIMESTAMP"),
    ],
)
