"""State models — snapshots, asset state."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field


class AssetState(BaseModel):
    """The persisted state of a single asset."""

    id: str
    type: str = ""
    fingerprint: str
    data: dict[str, Any] = Field(default_factory=dict)
    applied_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    applied_by: str = ""
    version: int = 1


class DependencyState(BaseModel):
    """The persisted state of a dependency edge."""

    source: str
    target: str
    type: str = ""
    fingerprint: str
    data: dict[str, Any] = Field(default_factory=dict)


class StateSnapshot(BaseModel):
    """Complete state for an environment."""

    version: int = 1
    environment: str = ""
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    assets: dict[str, AssetState] = Field(default_factory=dict)
    dependencies: list[DependencyState] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
