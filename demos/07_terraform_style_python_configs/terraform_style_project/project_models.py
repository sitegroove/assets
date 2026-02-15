"""Shared Pydantic asset models used by config files."""

from __future__ import annotations

from typing import cast

from assets import Asset, AssetField


class Service(Asset):
    """A microservice declared as a Python config object."""

    owner_team: str = ""
    language: str = "python"
    port: int = 8080
    replicas: int = cast(int, AssetField(default=1, fingerprint=False))
