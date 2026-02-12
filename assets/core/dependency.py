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
    """Field-level dependency entry (produced by lineage resolvers)."""

    source_asset: str
    source_field: str
    target_asset: str
    target_field: str
    transform: str | None = None
