"""Shared test fixtures."""

from __future__ import annotations

from typing import cast

import pytest

from assets import Asset, AssetField, Registry


class Column(Asset):
    type: str = ""
    description: str = ""
    pii: bool = False


class DataModel(Asset):
    sql: str | None = None
    columns: list[Column] = cast(
        list[Column],
        AssetField(default_factory=list, children=True),
    )
    row_count: int = cast(int, AssetField(default=0, fingerprint=False))


@pytest.fixture
def registry() -> Registry:
    return Registry()


@pytest.fixture
def sample_assets() -> list[Asset]:
    return [
        DataModel(id="raw.users", type="source", tags=["raw"]),
        DataModel(
            id="staging.users",
            type="data_model",
            tags=["staging", "pii"],
            sql="SELECT * FROM raw.users",
            depends_on=["raw.users"],
        ),
        DataModel(
            id="staging.payments",
            type="data_model",
            tags=["staging"],
            sql="SELECT * FROM raw.payments",
            depends_on=["raw.payments"],
        ),
        DataModel(id="raw.payments", type="source", tags=["raw"]),
        DataModel(
            id="mart.enriched",
            type="data_model",
            tags=["mart"],
            sql=(
                "SELECT u.*, p.amount "
                "FROM staging.users u "
                "JOIN staging.payments p ON u.id = p.user_id"
            ),
            depends_on=["staging.users", "staging.payments"],
        ),
    ]


@pytest.fixture
def populated_registry(registry: Registry, sample_assets: list[Asset]) -> Registry:
    for asset in sample_assets:
        registry.register(asset)
    return registry
