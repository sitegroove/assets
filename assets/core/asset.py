"""Base Asset model with fingerprinting and nested children."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from pydantic import BaseModel, computed_field

from assets.core.fields import FINGERPRINT_KEY


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
    """Base class for all assets in the registry.

    Assets can be nested to any depth via the ``children`` field.
    Each child is itself an Asset with its own identity, lineage, tags,
    and metadata — enabling hierarchies like database → schema → table → column.
    """

    # — identity —
    name: str
    kind: str = ""
    description: str = ""

    # — graph (populated by ref resolver, not user-set) —
    depends_on: list[str] = []

    # — content —
    sql: str | None = None

    # — classification —
    tags: list[str] = []
    metadata: dict[str, Any] = {}

    # — nested children —
    children: list[Asset] = []

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

    # — Child introspection —

    def list_children(self) -> list[str]:
        """List names of all direct children."""
        return [child.name for child in self.children]

    def get_child(self, name: str) -> Asset | None:
        """Look up a direct child by name."""
        return next((c for c in self.children if c.name == name), None)

    def get_child_at(self, path: str) -> Asset | None:
        """Look up a nested child by slash-separated path.

        Example::

            asset.get_child_at("public/users/email")
            # navigates: self → child "public" → child "users" → child "email"
        """
        parts = path.split("/")
        current: Asset | None = self
        for part in parts:
            if current is None:
                return None
            current = current.get_child(part)
        return current


# Resolve the self-referencing forward reference in children: list[Asset]
Asset.model_rebuild()
