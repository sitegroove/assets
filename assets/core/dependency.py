"""Dependency and FieldMapping models."""

from __future__ import annotations

import hashlib
from typing import Any

from pydantic import BaseModel, computed_field, model_validator

PATH_SEPARATOR = "/"


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
    """Lineage mapping between asset paths.

    Paths use '/' to separate the top-level asset name from the child path::

        source="raw.users/email"           → asset "raw.users", child "email"
        target="staging.users/email_clean"  → asset "staging.users", child "email_clean"
        source="db/public/users/email"      → asset "db", child path "public/users/email"
    """

    source: str
    target: str
    transform: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _migrate_legacy_fields(cls, data: Any) -> Any:
        """Accept the old 4-field constructor for backward compatibility."""
        if isinstance(data, dict):
            if "source_asset" in data and "source" not in data:
                sa = data.pop("source_asset")
                sf = data.pop("source_field", "")
                data["source"] = f"{sa}{PATH_SEPARATOR}{sf}" if sf else sa
            if "target_asset" in data and "target" not in data:
                ta = data.pop("target_asset")
                tf = data.pop("target_field", "")
                data["target"] = f"{ta}{PATH_SEPARATOR}{tf}" if tf else ta
        return data

    @property
    def source_asset(self) -> str:
        """Top-level asset name (everything before the first '/')."""
        return self.source.split(PATH_SEPARATOR, 1)[0]

    @property
    def source_field(self) -> str:
        """Child path after the asset name (everything after the first '/')."""
        parts = self.source.split(PATH_SEPARATOR, 1)
        return parts[1] if len(parts) > 1 else ""

    @property
    def target_asset(self) -> str:
        """Top-level asset name (everything before the first '/')."""
        return self.target.split(PATH_SEPARATOR, 1)[0]

    @property
    def target_field(self) -> str:
        """Child path after the asset name (everything after the first '/')."""
        parts = self.target.split(PATH_SEPARATOR, 1)
        return parts[1] if len(parts) > 1 else ""
