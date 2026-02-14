"""staging.orders model asset (declarative config)."""

from __future__ import annotations

from project_models import Column, DataModel

ASSET = DataModel(
    id="staging.orders",
    type="model",
    tags=["staging", "finance"],
    owner_team="analytics-engineering",
    materialization="table",
    sql=(
        "SELECT order_id, user_id, amount, status, ordered_at "
        "FROM raw.orders WHERE status <> 'cancelled'"
    ),
    depends_on=["raw.orders"],
    children=[
        Column(id="order_id", type="INTEGER"),
        Column(id="user_id", type="INTEGER"),
        Column(id="amount", type="DECIMAL"),
        Column(id="status", type="VARCHAR"),
        Column(id="ordered_at", type="TIMESTAMP"),
    ],
)
