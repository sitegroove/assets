"""Base Asset model with fingerprinting and dynamic child discovery."""

from __future__ import annotations

import hashlib
import json
from typing import Any, get_args, get_origin

from pydantic import BaseModel, Field, PrivateAttr, computed_field

from assets.core.fields import CHILDREN_KEY, FINGERPRINT_KEY


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


def _get_list_inner_type(annotation: Any) -> type | None:
    """Extract ``T`` from a ``list[T]`` annotation.

    Returns ``None`` when the annotation is not ``list[T]`` or the
    inner type is not a concrete class.
    """
    origin = get_origin(annotation)
    if origin is list:
        args = get_args(annotation)
        if args and isinstance(args[0], type):
            return args[0]
    return None


class Asset(BaseModel):
    """Base class for all assets in the registry.

    The base class is intentionally minimal and platform-agnostic.
    Domain-specific fields (``sql``, ``materialized``, ``project_id``,
    etc.) belong on consumer subclasses.

    **Children** are not a built-in field.  Instead, consumers declare
    one or more child fields using ``AssetField(children=True)``::

        class DataModel(Asset):
            columns: list[Column] = AssetField(
                default_factory=list, children=True,
            )
            metrics: list[MetricAsset] = AssetField(
                default_factory=list, children=True,
            )

    The framework discovers child fields dynamically and exposes them
    via :meth:`children`, :meth:`child`, and :meth:`child_at`.

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

    # — classification —
    tags: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)

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
        children_count = sum(len(items) for items in self._child_fields().values())
        parts = [f"id={self.id!r}"]
        if self.type:
            parts.append(f"type={self.type!r}")
        if children_count:
            parts.append(f"children={children_count}")
        return f"Asset({', '.join(parts)})"

    # — Child introspection —

    def _child_fields(self) -> dict[str, list[Asset]]:
        """Return ``{field_name: [asset, ...]}`` for all children fields.

        A field is a children field when its ``json_schema_extra``
        contains ``CHILDREN_KEY: True`` (set via
        ``AssetField(children=True)``).
        """
        result: dict[str, list[Asset]] = {}
        for attr_name, field_info in self.__class__.model_fields.items():
            extra = field_info.json_schema_extra or {}
            if isinstance(extra, dict) and extra.get(CHILDREN_KEY) is True:
                result[attr_name] = getattr(self, attr_name)
        return result

    def children(
        self,
        *,
        child_type: type[Asset] | None = None,
    ) -> list[Asset]:
        """All children across every ``AssetField(children=True)`` field.

        Args:
            child_type: When provided, only return children from fields
                whose ``list[T]`` annotation has ``T`` equal to (or a
                subclass of) *child_type*.

        Returns:
            Flat list of child asset objects.  Order follows field
            declaration order, then list order within each field.

        Example::

            model.children()                        # all children
            model.children(child_type=Column)        # only Column fields
            model.children(child_type=MetricAsset)   # only MetricAsset fields
        """
        result: list[Asset] = []
        for attr_name, field_info in self.__class__.model_fields.items():
            extra = field_info.json_schema_extra or {}
            if not (isinstance(extra, dict) and extra.get(CHILDREN_KEY) is True):
                continue
            if child_type is not None:
                inner = _get_list_inner_type(field_info.annotation)
                if inner is None or not issubclass(inner, child_type):
                    continue
            result.extend(getattr(self, attr_name))
        return result

    def child(self, path: str) -> Asset | None:
        """Look up a direct child by ``field_name/child_id``.

        Args:
            path: Slash-separated string ``"field_name/child_id"``.

        Returns:
            The child asset, or ``None`` if not found.

        Example::

            model.child("columns/email")
            model.child("metrics/revenue")
        """
        field_name, _, child_id = path.partition("/")
        if not child_id:
            return None
        children_map = self._child_fields()
        items = children_map.get(field_name, [])
        return next((c for c in items if c.id == child_id), None)

    def child_at(self, path: str) -> Asset | None:
        """Navigate nested children by ``field/id/field/id/...`` path.

        Each pair of segments is ``(field_name, child_id)``.  The path
        must therefore contain an even number of segments.

        Example::

            model.child_at("columns/email")
            model.child_at("columns/email/sub_columns/type")
        """
        parts = path.split("/")
        if len(parts) < 2 or len(parts) % 2 != 0:
            return None
        if any(p == "" for p in parts):
            return None
        current: Asset | None = self
        for i in range(0, len(parts), 2):
            if current is None:
                return None
            field_name = parts[i]
            child_id = parts[i + 1]
            current = current.child(f"{field_name}/{child_id}")
        return current
