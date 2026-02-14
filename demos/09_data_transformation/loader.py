"""Consumer-owned loaders for the dbt-style data transformation demo.

Three loaders implement the ``Loader`` protocol from the ``assets``
library:

- :class:`SourceLoader` — reads source YAML files (multi-table per file)
- :class:`ModelLoader`  — reads model YAML + companion SQL, renders
  Jinja2 templates, and wires ``depends_on``
- :class:`ExposureLoader` — reads exposure YAML files (multi-exposure
  per file)

The library handles discovery, diffing, caching, and registration.
The consumer owns parsing.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from assets import LoadedAsset

from models import Column, DataModel, Owner, Test
from utils import JinjaRenderer


# ── Source loader ─────────────────────────────────────────────────────


class SourceLoader:
    """Loads source YAML files containing one or more table definitions.

    Expected YAML format::

        source: fivetran_salesforce
        description: Salesforce data synced via Fivetran
        tables:
          - name: accounts
            id: raw_accounts
            description: Salesforce accounts
            columns:
              - id: account_id
                type: VARCHAR
              ...

    Each table becomes a separate :class:`DataModel` asset with
    ``type="source"`` and ``materialized="table"``.

    The loader also builds a **source mapping** that the
    :class:`JinjaRenderer` uses to resolve ``{{ source(...) }}``
    calls.
    """

    def __init__(self) -> None:
        self.source_mapping: dict[tuple[str, str], str] = {}

    def load(self, path: Path, root: Path) -> list[LoadedAsset]:
        """Parse a source YAML file and return LoadedAsset entries."""
        raw = yaml.safe_load(path.read_text())
        if not isinstance(raw, dict):
            return []

        source_name: str = raw.get("source", "")
        source_desc: str = raw.get("description", "")
        tables: list[dict[str, Any]] = raw.get("tables", [])
        assets: list[LoadedAsset] = []

        for table in tables:
            table_name: str = table.get("name", "")
            asset_id: str = table.get("id", f"{source_name}_{table_name}")

            # Register in the source mapping for Jinja rendering
            self.source_mapping[(source_name, table_name)] = asset_id

            # Build Column children
            children: list[dict[str, Any]] = []
            for col in table.get("columns", []):
                children.append(
                    {
                        "id": col["id"],
                        "type": col.get("type", "VARCHAR"),
                        "description": col.get("description", ""),
                        "pii": col.get("pii", False),
                        "classification": col.get("classification", "internal"),
                    }
                )

            data: dict[str, Any] = {
                "id": asset_id,
                "type": "source",
                "description": table.get("description", source_desc),
                "materialized": "table",
                "tags": table.get("tags", []),
                "children": children,
            }

            # Owner info from source level
            if "owner" in raw:
                data["owner"] = raw["owner"]

            asset = DataModel.model_validate(data)
            assets.append(LoadedAsset(asset=asset))

        return assets


# ── Model loader ──────────────────────────────────────────────────────


class ModelLoader:
    """Loads model YAML + companion SQL files with Jinja2 rendering.

    Expected layout::

        models/staging/
            stg_accounts.yml    <- metadata (id, type, columns, tests)
            stg_accounts.sql    <- SQL with {{ ref() }}, {{ source() }}

    The loader:
    1. Reads the YAML metadata
    2. Reads the companion ``.sql`` file (if any)
    3. Renders Jinja2 templates via :class:`JinjaRenderer`
    4. Extracts ``depends_on`` from captured ``ref()``/``source()`` calls
    5. Returns a :class:`LoadedAsset`

    Args:
        renderer: Pre-configured :class:`JinjaRenderer` with source
            mapping and variables already set.
    """

    def __init__(self, renderer: JinjaRenderer) -> None:
        self._renderer = renderer

    def load(self, path: Path, root: Path) -> list[LoadedAsset]:
        """Parse a model YAML + SQL pair and return a LoadedAsset."""
        raw = yaml.safe_load(path.read_text())
        if not isinstance(raw, dict):
            return []

        data: dict[str, Any] = dict(raw)

        # Read companion SQL file
        sql_path = path.with_suffix(".sql")
        if sql_path.exists():
            raw_sql = sql_path.read_text().strip()
            rendered = self._renderer.render(raw_sql)
            data["sql"] = rendered.sql

            # Merge dependencies from refs and sources
            depends_on: list[str] = list(
                dict.fromkeys(
                    data.get("depends_on", []) + rendered.refs + rendered.sources
                )
            )
            data["depends_on"] = depends_on

        asset = DataModel.model_validate(data)
        return [LoadedAsset(asset=asset)]


# ── Exposure loader ───────────────────────────────────────────────────


class ExposureLoader:
    """Loads exposure YAML files containing downstream consumers.

    Expected YAML format::

        exposures:
          - id: sales_dashboard
            description: Executive sales dashboard
            depends_on:
              - mart_sales_pipeline
              - mart_account_360
            owner:
              name: Sales Analytics Team
              email: sales-analytics@acme.com

    Each exposure becomes a :class:`DataModel` with
    ``type="exposure"`` and ``materialized="none"``.
    """

    def load(self, path: Path, root: Path) -> list[LoadedAsset]:
        """Parse an exposure YAML file and return LoadedAsset entries."""
        raw = yaml.safe_load(path.read_text())
        if not isinstance(raw, dict):
            return []

        exposures: list[dict[str, Any]] = raw.get("exposures", [])
        assets: list[LoadedAsset] = []

        for exp in exposures:
            data: dict[str, Any] = {
                "id": exp["id"],
                "type": "exposure",
                "description": exp.get("description", ""),
                "materialized": "none",
                "depends_on": exp.get("depends_on", []),
                "tags": exp.get("tags", []),
                "metadata": exp.get("metadata", {}),
            }

            if "owner" in exp:
                data["owner"] = exp["owner"]

            asset = DataModel.model_validate(data)
            assets.append(LoadedAsset(asset=asset))

        return assets
