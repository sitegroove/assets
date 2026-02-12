"""Base Asset model with fingerprinting and field-source introspection."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from pydantic import BaseModel, computed_field

from assets.core.fields import FIELD_NAME_KEY, FIELD_SOURCE_KEY, FINGERPRINT_KEY


def _serialize_value(val: Any) -> Any:
    """Serialize a value for canonical dict building, avoiding computed fields."""
    if isinstance(val, BaseModel):
        return {k: _serialize_value(v) for k, v in val.__dict__.items() if not k.startswith("_")}
    if isinstance(val, list):
        return [_serialize_value(item) for item in val]
    if isinstance(val, dict):
        return {k: _serialize_value(v) for k, v in val.items()}
    if isinstance(val, set):
        return sorted(_serialize_value(v) for v in val)
    return val


class Asset(BaseModel):
    """Base class for all assets in the registry."""

    # — identity —
    name: str
    kind: str = ""
    description: str = ""

    # — hierarchy —
    parent: str | None = None

    # — graph (populated by ref resolver, not user-set) —
    depends_on: list[str] = []

    # — content —
    sql: str | None = None

    # — classification —
    tags: list[str] = []
    metadata: dict[str, Any] = {}

    @property
    def local_name(self) -> str:
        """The unqualified name (last segment after '/')."""
        return self.name.rsplit("/", 1)[-1]

    @property
    def depth(self) -> int:
        """Nesting depth: 0 for top-level, 1 for direct child, etc."""
        return self.name.count("/")

    @computed_field  # type: ignore[prop-decorator]
    @property
    def fingerprint(self) -> str:
        """Deterministic SHA-256 hash of fingerprinted fields."""
        payload = self._canonical_dict()
        raw = json.dumps(payload, sort_keys=True, default=str)
        return hashlib.sha256(raw.encode()).hexdigest()

    def _canonical_dict(self) -> dict[str, Any]:
        """Dict of only fingerprinted fields.

        Builds dict manually from model_fields to avoid triggering
        computed_field recursion (model_dump() would call fingerprint property).
        """
        data: dict[str, Any] = {}
        for attr_name, field_info in self.__class__.model_fields.items():
            extra = field_info.json_schema_extra or {}
            if isinstance(extra, dict) and extra.get(FINGERPRINT_KEY) is False:
                continue
            data[attr_name] = _serialize_value(getattr(self, attr_name))
        return data

    # — Field source introspection —

    def get_field(self, field_name: str) -> BaseModel | None:
        """Look up a child field model by name across all field_source attributes.

        Matches against local_name for Asset children (whose names get
        qualified on registration), or falls back to the field_name_key.
        """
        for items, name_key in self._field_sources():
            match = next(
                (
                    f
                    for f in items
                    if getattr(f, "local_name", None) == field_name
                    or getattr(f, name_key, None) == field_name
                ),
                None,
            )
            if match is not None:
                return match
        return None

    def list_fields(self) -> list[str]:
        """List all field names from all field_source attributes.

        Uses local_name for Asset children (whose names get qualified
        on registration), preserving unqualified names for consumers.
        """
        result: list[str] = []
        for items, _name_key in self._field_sources():
            for f in items:
                if hasattr(f, "local_name"):
                    result.append(f.local_name)
                elif hasattr(f, _name_key):
                    result.append(getattr(f, _name_key))
        return result

    def _field_sources(self) -> list[tuple[list[Any], str]]:
        """Discover attributes marked as field_source via AssetField metadata."""
        sources: list[tuple[list[Any], str]] = []
        for attr_name, field_info in self.__class__.model_fields.items():
            extra = field_info.json_schema_extra or {}
            if isinstance(extra, dict) and extra.get(FIELD_SOURCE_KEY, False):
                items = getattr(self, attr_name, None) or []
                name_key = extra.get(FIELD_NAME_KEY, "name")
                sources.append((items, name_key))
        return sources
