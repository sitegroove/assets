"""Base Asset model with fingerprinting and nested children."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from pydantic import BaseModel, Field, PrivateAttr, computed_field

from assets.core.fields import FINGERPRINT_KEY


def _serialize_value(val: Any) -> Any:
    """Serialize a value for canonical dict building, avoiding computed fields."""
    if isinstance(val, BaseModel):
        canonical_builder = getattr(val, "_canonical_dict", None)
        if callable(canonical_builder):
            return canonical_builder()
        return {
            k: _serialize_value(v)
            for k, v in val.__dict__.items()
            if not k.startswith("_")
        }
    if isinstance(val, list):
        return [_serialize_value(item) for item in val]
    if isinstance(val, dict):
        return {k: _serialize_value(v) for k, v in val.items()}
    if isinstance(val, set):
        return sorted((_serialize_value(v) for v in val), key=str)
    return val


class Asset(BaseModel):
    """Base class for all assets in the registry.

    Assets can be nested to any depth via the ``children`` field.
    Each child is itself an Asset with its own identity, lineage, tags,
    and metadata — enabling hierarchies like database → schema → table → column.

    Assets are hashable (by ``id``) so they can be used in sets and
    as dict keys.  Two assets with the same ``id`` hash identically
    regardless of other fields — identity is determined by ``id`` alone.
    """

    model_config = {"frozen": False}

    # — identity —
    id: str
    type: str = ""
    description: str = ""

    # — graph —
    depends_on: list[str] = Field(default_factory=list)

    # — content —
    sql: str | None = None

    # — classification —
    tags: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)

    # — nested children —
    children: list[Asset] = Field(default_factory=list)

    # — cached fingerprint (private, excluded from serialisation) —
    # Thread-safety note: the cache write is a single pointer assignment
    # of an immutable str, which is atomic under CPython's GIL.  Under
    # free-threaded Python (PEP 703) the worst case is redundant
    # computation — two threads both compute the same deterministic hash
    # and both write the same value.  This is benign and intentionally
    # left unlocked to avoid per-asset lock overhead at scale.
    _fingerprint_cache: str | None = PrivateAttr(default=None)

    @computed_field
    @property
    def fingerprint(self) -> str:
        """Deterministic SHA-256 hash of fingerprinted fields (cached)."""
        if self._fingerprint_cache is not None:
            return self._fingerprint_cache
        payload = self._canonical_dict()
        raw = json.dumps(payload, sort_keys=True, default=str)
        self._fingerprint_cache = hashlib.sha256(raw.encode()).hexdigest()
        return self._fingerprint_cache

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

    def add_dependency(self, dependency: Asset | str) -> None:
        """Add one dependency by id, de-duplicated.

        If an :class:`Asset` is provided, its ``id`` is used.
        """
        dep_id: str
        if isinstance(dependency, Asset):
            dep_id = dependency.id
        else:
            dep_id = dependency
        dep_id = dep_id.strip()
        if not dep_id:
            raise ValueError("Dependency id must be a non-empty string")
        if dep_id not in self.depends_on:
            self.depends_on.append(dep_id)

    def __hash__(self) -> int:
        """Hash by id — enables use in sets and as dict keys."""
        return hash(self.id)

    def __eq__(self, other: object) -> bool:
        """Equality by id for consistency with __hash__."""
        if isinstance(other, Asset):
            return self.id == other.id
        return NotImplemented

    def __repr__(self) -> str:
        children_count = len(self.children)
        parts = [f"id={self.id!r}"]
        if self.type:
            parts.append(f"type={self.type!r}")
        if children_count:
            parts.append(f"children={children_count}")
        return f"Asset({', '.join(parts)})"

    # — Child introspection —

    def list_children(self) -> list[str]:
        """List names of all direct children."""
        return [child.id for child in self.children]

    def get_child(self, name: str) -> Asset | None:
        """Look up a direct child by name."""
        return next((c for c in self.children if c.id == name), None)

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
