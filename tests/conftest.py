"""Shared test fixtures."""

from __future__ import annotations

import pytest
from pydantic import BaseModel

from assets import Asset, AssetField, Registry


class Column(BaseModel):
    name: str
    type: str = ""
    description: str = ""
    pii: bool = False


class DataModel(Asset):
    columns: list[Column] = AssetField(default_factory=list, field_source=True)
    row_count: int = AssetField(default=0, fingerprint=False)


@pytest.fixture
def registry() -> Registry:
    return Registry()


@pytest.fixture
def sample_assets() -> list[Asset]:
    return [
        Asset(name="raw.users", kind="source", tags=["raw"]),
        Asset(
            name="staging.users",
            kind="data_model",
            tags=["staging", "pii"],
            sql="SELECT * FROM {{ ref('raw.users') }}",
        ),
        Asset(
            name="staging.payments",
            kind="data_model",
            tags=["staging"],
            sql="SELECT * FROM {{ ref('raw.payments') }}",
        ),
        Asset(name="raw.payments", kind="source", tags=["raw"]),
        Asset(
            name="mart.enriched",
            kind="data_model",
            tags=["mart"],
            sql=(
                "SELECT u.*, p.amount "
                "FROM {{ ref('staging.users') }} u "
                "JOIN {{ ref('staging.payments') }} p ON u.id = p.user_id"
            ),
        ),
    ]


@pytest.fixture
def populated_registry(registry: Registry, sample_assets: list[Asset]) -> Registry:
    for asset in sample_assets:
        registry.register(asset)
    return registry
