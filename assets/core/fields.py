"""Custom Pydantic Field wrapper carrying asset metadata."""

from __future__ import annotations

from typing import Any

from pydantic import Field
from pydantic.fields import FieldInfo

FINGERPRINT_KEY = "fingerprint"
FIELD_SOURCE_KEY = "field_source"
FIELD_NAME_KEY = "field_name_key"
CHILD_KIND_KEY = "child_kind"


def AssetField(  # noqa: N802
    default: Any = ...,
    *,
    fingerprint: bool = True,
    field_source: bool = False,
    field_name_key: str = "name",
    child_kind: str = "field",
    **kwargs: Any,
) -> FieldInfo:
    """Create a Pydantic FieldInfo with asset-specific metadata.

    Args:
        default: Default value for the field.
        fingerprint: Include in identity hash.
        field_source: Whether this attribute holds resolvable child Asset models.
        field_name_key: Key on child model used for name resolution.
        child_kind: Default kind assigned to children from this field source.
    """
    json_schema_extra = kwargs.pop("json_schema_extra", {}) or {}
    json_schema_extra[FINGERPRINT_KEY] = fingerprint
    json_schema_extra[FIELD_SOURCE_KEY] = field_source
    json_schema_extra[FIELD_NAME_KEY] = field_name_key
    json_schema_extra[CHILD_KIND_KEY] = child_kind
    return Field(
        default=default,
        json_schema_extra=json_schema_extra,
        **kwargs,
    )
