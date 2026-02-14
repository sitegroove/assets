"""Shared test fixtures."""

from __future__ import annotations

import pytest

from assets import Asset, AssetField, Registry


class Column(Asset):
    type: str = ""
    description: str = ""
    pii: bool = False


class DataModel(Asset):
    row_count: int = AssetField(default=0, fingerprint=False)


@pytest.fixture
def registry() -> Registry:
    return Registry()


@pytest.fixture
def sample_assets() -> list[Asset]:
    return [
        Asset(id="raw.users", type="source", tags=["raw"]),
        Asset(
            id="staging.users",
            type="data_model",
            tags=["staging", "pii"],
            sql="SELECT * FROM raw.users",
            depends_on=["raw.users"],
        ),
        Asset(
            id="staging.payments",
            type="data_model",
            tags=["staging"],
            sql="SELECT * FROM raw.payments",
            depends_on=["raw.payments"],
        ),
        Asset(id="raw.payments", type="source", tags=["raw"]),
        Asset(
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
