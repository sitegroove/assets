"""staging.users model asset (declarative config)."""

from __future__ import annotations

from project_models import Column, DataModel

ASSET = DataModel(
    id="staging.users",
    type="model",
    tags=["staging", "pii"],
    owner_team="analytics-engineering",
    materialization="table",
    sql=(
        "SELECT user_id, LOWER(TRIM(email)) AS email_clean, country, created_at "
        "FROM raw.users"
    ),
    depends_on=["raw.users"],
    children=[
        Column(id="user_id", type="INTEGER"),
        Column(id="email_clean", type="VARCHAR", pii=True),
        Column(id="country", type="VARCHAR"),
        Column(id="created_at", type="TIMESTAMP"),
    ],
)
