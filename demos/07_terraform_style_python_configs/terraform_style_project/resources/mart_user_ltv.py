"""mart.user_ltv mart asset (declarative config)."""

from __future__ import annotations

from project_models import Column, DataModel

ASSET = DataModel(
    id="mart.user_ltv",
    type="mart",
    tags=["mart", "finance"],
    owner_team="growth-analytics",
    materialization="table",
    sql=(
        "SELECT u.user_id, u.email_clean, u.country, "
        "SUM(o.amount) AS lifetime_value, COUNT(*) AS order_count "
        "FROM staging.users u "
        "JOIN staging.orders o ON u.user_id = o.user_id "
        "GROUP BY u.user_id, u.email_clean, u.country"
    ),
    depends_on=["staging.users", "staging.orders"],
    children=[
        Column(id="user_id", type="INTEGER"),
        Column(id="email_clean", type="VARCHAR", pii=True),
        Column(id="country", type="VARCHAR"),
        Column(id="lifetime_value", type="DECIMAL"),
        Column(id="order_count", type="INTEGER"),
    ],
)
