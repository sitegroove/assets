"""Domain models for the dbt-style data transformation demo.

Defines consumer-owned Pydantic models that extend the base ``Asset``
class.  These are not part of the ``assets`` library — they live in
consumer code and can be customised freely.
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel

from assets import Asset, AssetField


# ── Classification ────────────────────────────────────────────────────


class Classification(str, Enum):
    """Data sensitivity classification."""

    PUBLIC = "public"
    INTERNAL = "internal"
    CONFIDENTIAL = "confidential"
    RESTRICTED = "restricted"


# ── Column (nested asset) ────────────────────────────────────────────


class Column(Asset):
    """A typed column with optional PII flag and classification.

    Columns are nested assets — they live inside their parent asset's
    ``columns`` list and participate in column-level lineage.
    """

    type: str = "VARCHAR"
    pii: bool = False
    classification: Classification = Classification.INTERNAL


# ── Supporting BaseModel types ────────────────────────────────────────


class Test(BaseModel):
    """A data quality test attached to a model."""

    name: str
    type: str = "not_null"
    column: str | None = None
    severity: str = "error"
    config: dict[str, Any] = {}


class Metric(BaseModel):
    """A business metric derived from a model."""

    name: str
    expression: str
    description: str = ""
    time_grain: str = "day"


class Owner(BaseModel):
    """Team or person responsible for an asset."""

    name: str
    email: str = ""
    team: str = ""


# ── DataModel (primary asset type) ───────────────────────────────────


class DataModel(Asset):
    """The consumer's primary asset type — an analytical data model.

    Extends ``Asset`` with metrics, tests, ownership, materialisation
    strategy, and freshness SLAs.  Columns are stored in the
    ``columns`` field as nested :class:`Column` assets declared with
    ``AssetField(children=True)``.
    """

    columns: list[Column] = AssetField(default_factory=list, children=True)
    sql: str | None = None
    metrics: list[Metric] = AssetField(default_factory=list)
    tests: list[Test] = AssetField(default_factory=list)
    owner: Owner | None = AssetField(default=None, fingerprint=False)
    materialized: str = "view"
    freshness_hours: int | None = AssetField(default=None, fingerprint=False)
    row_count: int = AssetField(default=0, fingerprint=False)
