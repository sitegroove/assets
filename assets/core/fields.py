"""Custom Pydantic Field wrapper carrying asset metadata."""

from __future__ import annotations

from typing import Any

from pydantic import Field
from pydantic.fields import FieldInfo

FINGERPRINT_KEY = "fingerprint"
CHILDREN_KEY = "children"


def AssetField(  # noqa: N802
    default: Any = ...,
    *,
    fingerprint: bool = True,
    children: bool = False,
    **kwargs: Any,
) -> FieldInfo:
    """Create a Pydantic FieldInfo with asset-specific metadata.

    Args:
        default: Default value for the field.
        fingerprint: Include in identity hash.
        children: Mark this field as containing child assets.
            The field must be typed as ``list[SomeAsset]``.
            Multiple fields on a single asset can be marked
            ``children=True`` — the framework discovers them
            dynamically via introspection.
    """
    json_schema_extra = kwargs.pop("json_schema_extra", {}) or {}
    json_schema_extra[FINGERPRINT_KEY] = fingerprint
    json_schema_extra[CHILDREN_KEY] = children
    return Field(
        default=default,
        json_schema_extra=json_schema_extra,
        **kwargs,
    )
