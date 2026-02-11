"""State models — snapshots, asset state, source file refs."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field


class SourceFileRef(BaseModel):
    """Reference to a source file that produced an asset."""

    path: str
    content_hash: str
    sql_path: str | None = None
    sql_content_hash: str | None = None


class AssetState(BaseModel):
    """The persisted state of a single asset."""

    name: str
    kind: str = ""
    fingerprint: str
    data: dict[str, Any] = {}
    source_files: list[SourceFileRef] = []
    applied_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    applied_by: str = ""
    version: int = 1
    deleted: bool = False


class DependencyState(BaseModel):
    """The persisted state of a dependency edge."""

    source: str
    target: str
    type: str = ""
    fingerprint: str
    data: dict[str, Any] = {}


class StateSnapshot(BaseModel):
    """Complete state for an environment."""

    version: int = 1
    environment: str = ""
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    assets: dict[str, AssetState] = {}
    dependencies: list[DependencyState] = []
    metadata: dict[str, Any] = {}
