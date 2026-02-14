"""Shared Pydantic asset models used by config files."""

from __future__ import annotations

from assets import Asset, AssetField


class Owner(Asset):
    team: str = ""
    email: str = ""


class Column(Asset):
    type: str = ""
    pii: bool = False


class DataModel(Asset):
    materialization: str = "view"
    owner_team: str = ""
    row_count: int = AssetField(default=0, fingerprint=False)
