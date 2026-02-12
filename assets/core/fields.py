"""Custom Pydantic Field wrapper carrying asset metadata."""

from __future__ import annotations

from typing import Any

from pydantic import Field
from pydantic.fields import FieldInfo

FINGERPRINT_KEY = "fingerprint"


def AssetField(  # noqa: N802
    default: Any = ...,
    *,
    fingerprint: bool = True,
    **kwargs: Any,
) -> FieldInfo:
    """Create a Pydantic FieldInfo with asset-specific metadata.

    Args:
        default: Default value for the field.
        fingerprint: Include in identity hash.
    """
    json_schema_extra = kwargs.pop("json_schema_extra", {}) or {}
    json_schema_extra[FINGERPRINT_KEY] = fingerprint
    return Field(
        default=default,
        json_schema_extra=json_schema_extra,
        **kwargs,
    )
