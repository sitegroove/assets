"""Dependency and FieldMapping models."""

from __future__ import annotations

import hashlib
from typing import Any

from pydantic import BaseModel, computed_field


class Dependency(BaseModel):
    """An edge in the asset graph: source (upstream) → target (downstream)."""

    source: str
    target: str
    type: str = "ref"
    metadata: dict[str, Any] = {}

    @computed_field  # type: ignore[prop-decorator]
    @property
    def fingerprint(self) -> str:
        raw = f"{self.source}:{self.target}:{self.type}"
        return hashlib.sha256(raw.encode()).hexdigest()


class FieldMapping(BaseModel):
    """Column-level lineage entry (produced by lineage resolvers)."""

    source_asset: str
    source_field: str
    target_asset: str
    target_field: str
    transform: str | None = None

    @property
    def source_path(self) -> str:
        """Full qualified path: source_asset/source_field."""
        return f"{self.source_asset}/{self.source_field}"

    @property
    def target_path(self) -> str:
        """Full qualified path: target_asset/target_field."""
        return f"{self.target_asset}/{self.target_field}"
