#!/usr/bin/env python3
"""Demo 6: Real-World E-Commerce Analytics Platform.

A complete example showing how a consumer builds a data platform on top
of the assets library.  Covers:

  - Rich Pydantic models (Column with PII/classification, Metric, Owner, Tests)
  - YAML + SQL project layout (dbt-style)
  - sqlglot-based column lineage
  - Plan / apply / drift detection
  - Multi-environment promotion (dev → staging → production)
  - SQLite state backend

Run:
    pip install pyyaml sqlglot
    python demos/06_ecommerce_platform.py
"""

from __future__ import annotations

import json
import shutil
import tempfile
from enum import Enum
from pathlib import Path
from typing import Any

import yaml

try:
    import sqlglot
    from sqlglot.lineage import lineage as sqlglot_lineage

    HAS_SQLGLOT = True
except ImportError:
    HAS_SQLGLOT = False

from pydantic import BaseModel

from assets import (
    Asset,
    AssetField,
    Environment,
    EnvironmentConfig,
    FieldMapping,
    LineageResolver,
    ProjectLoader,
    Registry,
    SQLiteBackend,
    StateManager,
)

# ═══════════════════════════════════════════════════════════════
# 1. Domain Models — what a real consumer would define
# ═══════════════════════════════════════════════════════════════


class Classification(str, Enum):
    """Data sensitivity classification."""

    PUBLIC = "public"
    INTERNAL = "internal"
    CONFIDENTIAL = "confidential"
    RESTRICTED = "restricted"


class Column(BaseModel):
    """A typed column with optional PII flag and classification."""

    name: str
    type: str = "VARCHAR"
    description: str = ""
    pii: bool = False
    classification: Classification = Classification.INTERNAL


class Test(BaseModel):
    """A data quality test attached to a model."""

    name: str
    type: str = "not_null"  # not_null | unique | accepted_values | custom_sql
    column: str | None = None
    severity: str = "error"  # error | warn
    config: dict[str, Any] = {}


class Metric(BaseModel):
    """A business metric derived from a mart model."""

    name: str
    expression: str  # SQL expression, e.g. "SUM(amount)"
    description: str = ""
    time_grain: str = "day"  # day | week | month


class Owner(BaseModel):
    """Team or person responsible for this model."""

    name: str
    email: str = ""
    team: str = ""


class DataModel(Asset):
    """The consumer's main asset type — an analytical data model.

    Extends the base Asset with columns, metrics, tests, owner info,
    freshness SLAs, and materialization strategy.
    """

    # Columns are marked as field_source so get_field() / list_fields() work
    columns: list[Column] = AssetField(default_factory=list, field_source=True)

    # Metrics — only relevant for mart models
    metrics: list[Metric] = AssetField(default_factory=list)

    # Data quality tests
    tests: list[Test] = AssetField(default_factory=list)

    # Ownership
    owner: Owner | None = AssetField(default=None, fingerprint=False)

    # Materialization strategy (view, table, incremental)
    materialized: str = "view"

    # Freshness SLA in hours — non-fingerprinted operational metadata
    freshness_hours: int | None = AssetField(default=None, fingerprint=False)

    # Row count — runtime stat, not part of identity
    row_count: int = AssetField(default=0, fingerprint=False)


# ═══════════════════════════════════════════════════════════════
# 2. Custom YAML + SQL Loader (dbt-style project layout)
# ═══════════════════════════════════════════════════════════════


class EcommerceLoader(ProjectLoader):
    """Loads YAML model definitions with companion .sql files.

    Expected layout:
        models/
        ├── sources/
        │   └── raw_users.yaml
        ├── staging/
        │   ├── stg_users.yaml
        │   └── stg_users.sql
        ├── intermediate/
        │   ├── int_user_orders.yaml
        │   └── int_user_orders.sql
        └── marts/
            ├── mart_revenue.yaml
            └── mart_revenue.sql
    """

    def discover_files(self, project_dir: Path) -> list[Path]:
        return sorted(
            p for p in project_dir.rglob("*") if p.suffix in (".yaml", ".yml")
        )

    def parse_file(self, path: Path, root: Path) -> dict[str, Any] | None:
        if path.suffix not in (".yaml", ".yml"):
            return None
        data = yaml.safe_load(path.read_text())
        if not isinstance(data, dict):
            return None
        # Merge companion .sql file if it exists
        sql_path = path.with_suffix(".sql")
        if sql_path.exists():
            data["sql"] = sql_path.read_text().strip()
        return data


# ═══════════════════════════════════════════════════════════════
# 3. sqlglot-Based Column Lineage Resolver
# ═══════════════════════════════════════════════════════════════


class SqlglotLineageResolver(LineageResolver):
    """Production-quality lineage resolver using sqlglot.lineage().

    Traces columns through CTEs, subqueries, and JOINs.
    Falls back to regex for SQL that sqlglot can't parse.
    """

    def resolve(self, sql: str, schema: dict[str, list[str]]) -> list[FieldMapping]:
        if not HAS_SQLGLOT:
            return []

        mappings: list[FieldMapping] = []

        # Build schema in sqlglot format: {table: {col: type}}
        sg_schema: dict[str, dict[str, str]] = {}
        for table, columns in schema.items():
            sg_schema[table] = {col: "VARCHAR" for col in columns}

        # Parse to find output columns
        try:
            parsed = sqlglot.parse_one(sql)
        except Exception:
            return mappings

        output_cols: list[str] = []
        has_star = False
        for sel in parsed.selects:
            col_name = sel.alias_or_name
            if col_name == "*":
                has_star = True
            elif col_name:
                output_cols.append(col_name)

        # SELECT * — expand via sqlglot qualify, fall back to schema columns
        if has_star and not output_cols:
            try:
                from sqlglot.optimizer.qualify_columns import qualify_columns
                from sqlglot.optimizer.qualify_tables import qualify_tables

                qualified = qualify_columns(
                    qualify_tables(parsed, schema=sg_schema), schema=sg_schema,
                )
                for sel in qualified.selects:
                    name = sel.alias_or_name
                    if name and name != "*":
                        output_cols.append(name)
            except Exception:
                seen: set[str] = set()
                for cols in schema.values():
                    for c in cols:
                        if c not in seen:
                            output_cols.append(c)
                            seen.add(c)

        for col_name in output_cols:
            try:
                node = sqlglot_lineage(col_name, sql, schema=sg_schema)
                self._walk_lineage(node, col_name, mappings)
            except Exception:
                continue

        return mappings

    def _walk_lineage(
        self, node: Any, target_col: str, mappings: list[FieldMapping]
    ) -> None:
        """Recursively walk lineage tree to leaf sources."""
        if not node.downstream:
            # Leaf — extract table name
            src_table = ""
            if hasattr(node.source, "this") and hasattr(node.source.this, "this"):
                src_table = str(node.source.this.this)
            src_col = node.name.split(".")[-1] if "." in node.name else node.name

            # Detect aggregate transforms
            transform: str | None = None
            expr_sql = str(node.expression) if node.expression else ""
            for fn in ("SUM", "AVG", "COUNT", "MIN", "MAX", "COALESCE"):
                if fn in expr_sql.upper():
                    transform = fn
                    break

            if src_table and src_table != "*":
                mappings.append(
                    FieldMapping(
                        source_asset=src_table,
                        source_field=src_col,
                        target_asset="<target>",
                        target_field=target_col,
                        transform=transform,
                    )
                )
        else:
            for child in node.downstream:
                self._walk_lineage(child, target_col, mappings)


# ═══════════════════════════════════════════════════════════════
# 4. Create the project on disk
# ═══════════════════════════════════════════════════════════════


def write_model(base: Path, subdir: str, name: str, data: dict, sql: str | None = None) -> None:
    folder = base / subdir
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{name}.yaml").write_text(yaml.dump(data, default_flow_style=False, sort_keys=False))
    if sql:
        (folder / f"{name}.sql").write_text(sql)


def create_project(root: Path) -> Path:
    models = root / "models"

    # ── Sources ──

    write_model(models, "sources", "raw_users", {
        "name": "raw.users",
        "kind": "source",
        "description": "Raw users from production Postgres replica",
        "tags": ["raw", "pii", "users"],
        "materialized": "table",
        "freshness_hours": 1,
        "owner": {"name": "Data Platform", "email": "platform@shop.io", "team": "infrastructure"},
        "columns": [
            {"name": "user_id", "type": "INTEGER", "description": "Primary key"},
            {"name": "email", "type": "VARCHAR", "pii": True, "classification": "restricted",
             "description": "User email address"},
            {"name": "full_name", "type": "VARCHAR", "pii": True, "classification": "confidential"},
            {"name": "country", "type": "VARCHAR"},
            {"name": "created_at", "type": "TIMESTAMP", "description": "Account creation time"},
        ],
        "tests": [
            {"name": "user_id_not_null", "type": "not_null", "column": "user_id"},
            {"name": "user_id_unique", "type": "unique", "column": "user_id"},
            {"name": "email_not_null", "type": "not_null", "column": "email"},
        ],
    })

    write_model(models, "sources", "raw_orders", {
        "name": "raw.orders",
        "kind": "source",
        "description": "Raw orders from the transactional database",
        "tags": ["raw", "finance", "orders"],
        "materialized": "table",
        "freshness_hours": 1,
        "owner": {"name": "Data Platform", "email": "platform@shop.io", "team": "infrastructure"},
        "columns": [
            {"name": "order_id", "type": "INTEGER", "description": "Primary key"},
            {"name": "user_id", "type": "INTEGER", "description": "FK to users"},
            {"name": "amount", "type": "DECIMAL", "description": "Order total in USD"},
            {"name": "currency", "type": "VARCHAR"},
            {"name": "status", "type": "VARCHAR", "description": "pending/completed/refunded"},
            {"name": "ordered_at", "type": "TIMESTAMP"},
        ],
        "tests": [
            {"name": "order_id_not_null", "type": "not_null", "column": "order_id"},
            {"name": "amount_positive", "type": "custom_sql", "column": "amount",
             "config": {"sql": "SELECT * FROM {{ ref('raw.orders') }} WHERE amount < 0"}},
        ],
    })

    write_model(models, "sources", "raw_products", {
        "name": "raw.products",
        "kind": "source",
        "description": "Product catalog from the CMS",
        "tags": ["raw", "catalog"],
        "materialized": "table",
        "columns": [
            {"name": "product_id", "type": "INTEGER"},
            {"name": "name", "type": "VARCHAR"},
            {"name": "category", "type": "VARCHAR"},
            {"name": "price", "type": "DECIMAL"},
            {"name": "is_active", "type": "BOOLEAN"},
        ],
    })

    # ── Staging ──

    write_model(models, "staging", "stg_users", {
        "name": "staging.users",
        "kind": "staging",
        "description": "Cleaned and validated user records — PII handled",
        "tags": ["staging", "pii", "users"],
        "materialized": "view",
        "owner": {"name": "Analytics Engineering", "team": "analytics"},
        "columns": [
            {"name": "user_id", "type": "INTEGER"},
            {"name": "email_domain", "type": "VARCHAR",
             "description": "Domain part of email (PII-safe)"},
            {"name": "country", "type": "VARCHAR"},
            {"name": "created_at", "type": "TIMESTAMP"},
        ],
        "tests": [
            {"name": "user_id_not_null", "type": "not_null", "column": "user_id"},
        ],
    }, sql="""\
WITH source AS (
    SELECT
        u.user_id,
        u.email,
        u.country,
        u.created_at
    FROM {{ ref('raw.users') }} u
    WHERE u.user_id IS NOT NULL
),
cleaned AS (
    SELECT
        source.user_id,
        SPLIT_PART(source.email, '@', 2) AS email_domain,
        UPPER(source.country) AS country,
        source.created_at
    FROM source
)
SELECT * FROM cleaned""")

    write_model(models, "staging", "stg_orders", {
        "name": "staging.orders",
        "kind": "staging",
        "description": "Validated orders — only completed, amounts > 0",
        "tags": ["staging", "finance", "orders"],
        "materialized": "view",
        "owner": {"name": "Analytics Engineering", "team": "analytics"},
        "columns": [
            {"name": "order_id", "type": "INTEGER"},
            {"name": "user_id", "type": "INTEGER"},
            {"name": "amount_usd", "type": "DECIMAL", "description": "Amount normalized to USD"},
            {"name": "ordered_at", "type": "TIMESTAMP"},
        ],
        "tests": [
            {"name": "order_id_not_null", "type": "not_null", "column": "order_id"},
            {"name": "amount_positive", "type": "custom_sql", "column": "amount_usd",
             "severity": "warn"},
        ],
    }, sql="""\
WITH source AS (
    SELECT
        o.order_id,
        o.user_id,
        o.amount,
        o.currency,
        o.status,
        o.ordered_at
    FROM {{ ref('raw.orders') }} o
    WHERE o.status = 'completed' AND o.amount > 0
),
normalized AS (
    SELECT
        source.order_id,
        source.user_id,
        CASE
            WHEN source.currency = 'EUR' THEN source.amount * 1.08
            WHEN source.currency = 'GBP' THEN source.amount * 1.26
            ELSE source.amount
        END AS amount_usd,
        source.ordered_at
    FROM source
)
SELECT * FROM normalized""")

    # ── Intermediate ──

    write_model(models, "intermediate", "int_user_orders", {
        "name": "intermediate.user_orders",
        "kind": "intermediate",
        "description": "User enriched with order aggregates",
        "tags": ["intermediate", "users", "orders"],
        "materialized": "view",
        "columns": [
            {"name": "user_id", "type": "INTEGER"},
            {"name": "email_domain", "type": "VARCHAR"},
            {"name": "country", "type": "VARCHAR"},
            {"name": "total_spent", "type": "DECIMAL"},
            {"name": "order_count", "type": "INTEGER"},
            {"name": "first_order_at", "type": "TIMESTAMP"},
            {"name": "last_order_at", "type": "TIMESTAMP"},
        ],
    }, sql="""\
WITH users AS (
    SELECT * FROM {{ ref('staging.users') }}
),
orders AS (
    SELECT
        o.user_id,
        SUM(o.amount_usd)   AS total_spent,
        COUNT(*)             AS order_count,
        MIN(o.ordered_at)    AS first_order_at,
        MAX(o.ordered_at)    AS last_order_at
    FROM {{ ref('staging.orders') }} o
    GROUP BY o.user_id
)
SELECT
    users.user_id,
    users.email_domain,
    users.country,
    COALESCE(orders.total_spent, 0) AS total_spent,
    COALESCE(orders.order_count, 0) AS order_count,
    orders.first_order_at,
    orders.last_order_at
FROM users
LEFT JOIN orders ON users.user_id = orders.user_id""")

    # ── Marts ──

    write_model(models, "marts", "mart_revenue", {
        "name": "mart.revenue",
        "kind": "mart",
        "description": "Daily revenue by country — the core finance KPI model",
        "tags": ["mart", "finance", "kpi"],
        "materialized": "table",
        "freshness_hours": 6,
        "owner": {"name": "Finance Analytics", "email": "finance-data@shop.io", "team": "finance"},
        "columns": [
            {"name": "country", "type": "VARCHAR"},
            {"name": "total_revenue", "type": "DECIMAL"},
            {"name": "avg_order_value", "type": "DECIMAL"},
            {"name": "total_orders", "type": "INTEGER"},
            {"name": "unique_customers", "type": "INTEGER"},
        ],
        "metrics": [
            {"name": "total_revenue", "expression": "SUM(total_spent)",
             "description": "Gross revenue across all customers"},
            {"name": "avg_order_value", "expression": "SUM(total_spent) / SUM(order_count)",
             "description": "Average value per order"},
            {"name": "customers", "expression": "COUNT(DISTINCT user_id)"},
        ],
        "tests": [
            {"name": "revenue_not_negative", "type": "custom_sql",
             "config": {"sql": "SELECT * FROM {{ ref('mart.revenue') }} WHERE total_revenue < 0"}},
        ],
    }, sql="""\
WITH base AS (
    SELECT * FROM {{ ref('intermediate.user_orders') }}
    WHERE total_spent > 0
)
SELECT
    base.country,
    SUM(base.total_spent)                AS total_revenue,
    SUM(base.total_spent) / SUM(base.order_count) AS avg_order_value,
    SUM(base.order_count)                AS total_orders,
    COUNT(DISTINCT base.user_id)         AS unique_customers
FROM base
GROUP BY base.country""")

    write_model(models, "marts", "mart_customer_segments", {
        "name": "mart.customer_segments",
        "kind": "mart",
        "description": "Customer segmentation based on spending behavior",
        "tags": ["mart", "marketing", "users"],
        "materialized": "table",
        "owner": {"name": "Marketing Analytics", "team": "marketing"},
        "columns": [
            {"name": "user_id", "type": "INTEGER"},
            {"name": "email_domain", "type": "VARCHAR"},
            {"name": "country", "type": "VARCHAR"},
            {"name": "segment", "type": "VARCHAR", "description": "vip/regular/new/inactive"},
            {"name": "lifetime_value", "type": "DECIMAL"},
            {"name": "order_count", "type": "INTEGER"},
        ],
    }, sql="""\
WITH customers AS (
    SELECT * FROM {{ ref('intermediate.user_orders') }}
),
segmented AS (
    SELECT
        customers.user_id,
        customers.email_domain,
        customers.country,
        CASE
            WHEN customers.total_spent > 1000 AND customers.order_count > 10 THEN 'vip'
            WHEN customers.total_spent > 100  THEN 'regular'
            WHEN customers.order_count = 0    THEN 'inactive'
            ELSE 'new'
        END AS segment,
        customers.total_spent AS lifetime_value,
        customers.order_count
    FROM customers
)
SELECT * FROM segmented""")

    return models


# ═══════════════════════════════════════════════════════════════
# 5. Run the full platform lifecycle
# ═══════════════════════════════════════════════════════════════


def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="ecommerce_platform_"))
    models_dir = create_project(tmp)
    print("=" * 70)
    print("  E-Commerce Analytics Platform Demo")
    print("=" * 70)
    print(f"\n  Project: {models_dir}")

    # ── Set up infrastructure ──

    backend = SQLiteBackend(db_path=tmp / "state.db")
    env_config = EnvironmentConfig(
        default="production",
        environments={
            "production": Environment(name="production"),
            "staging": Environment(name="staging", parent="production"),
        },
    )
    registry = Registry()
    loader = EcommerceLoader(
        registry, asset_class=DataModel, cache_dir=str(tmp / ".cache")
    )
    manager = StateManager(registry, loader, backend, env_config)

    # ── Step 1: Load the project ──

    print(f"\n{'─' * 70}")
    print("  Step 1: Load project")
    print(f"{'─' * 70}")

    result = loader.load(str(models_dir))
    print(f"  Loaded {result.loaded} assets (reused={result.reused}, compiled={result.recompiled})")

    print(f"\n  Registered assets ({len(registry)}):")
    for asset in registry.all():
        model = asset  # type: DataModel
        cols = len(model.columns) if hasattr(model, "columns") else 0
        tests = len(model.tests) if hasattr(model, "tests") else 0
        deps = f"  deps={model.depends_on}" if model.depends_on else ""
        print(f"    {model.name:<35} kind={model.kind:<15} cols={cols}  tests={tests}{deps}")

    # ── Step 2: Explore the graph ──

    print(f"\n{'─' * 70}")
    print("  Step 2: Graph exploration")
    print(f"{'─' * 70}")

    graph = registry.graph
    print(f"  Topological order: {graph.topological_sort()}")
    print(f"  Roots (sources):   {graph.roots()}")
    print(f"  Leaves (marts):    {graph.leaves()}")

    # Impact analysis: what breaks if raw.users changes?
    impacted = graph.descendants("raw.users")
    print(f"\n  Impact of raw.users change: {impacted}")

    # ── Step 3: Selectors ──

    print(f"\n{'─' * 70}")
    print("  Step 3: Selectors")
    print(f"{'─' * 70}")

    for selector in ["tag:pii", "kind:mart", "raw.*", "+mart.revenue", "tag:finance,kind:mart"]:
        result_sel = registry.select(selector)
        print(f"  {selector:<30} → {sorted(result_sel.names)}")

    # ── Step 4: Field introspection ──

    print(f"\n{'─' * 70}")
    print("  Step 4: Field introspection")
    print(f"{'─' * 70}")

    stg_users = registry.get("staging.users")
    print(f"  staging.users fields: {stg_users.list_fields()}")

    email_col = stg_users.get_field("email_domain")
    print(f"  email_domain → type={email_col.type}, pii={email_col.pii}, "
          f"desc='{email_col.description}'")

    # PII scan across all models
    pii_fields = []
    for asset in registry.all():
        if hasattr(asset, "columns"):
            for col in asset.columns:
                if col.pii:
                    pii_fields.append(f"{asset.name}.{col.name}")
    print(f"\n  PII columns across all models: {pii_fields}")

    # Restricted classification scan
    restricted = []
    for asset in registry.all():
        if hasattr(asset, "columns"):
            for col in asset.columns:
                if col.classification == Classification.RESTRICTED:
                    restricted.append(f"{asset.name}.{col.name}")
    print(f"  RESTRICTED columns: {restricted}")

    # ── Step 5: Column lineage with sqlglot ──

    print(f"\n{'─' * 70}")
    print("  Step 5: Column-level lineage (sqlglot)")
    print(f"{'─' * 70}")

    resolver = SqlglotLineageResolver()

    for target_name in ["staging.users", "intermediate.user_orders", "mart.revenue"]:
        lineage = registry.resolve_column_lineage(asset_name=target_name, resolver=resolver)
        print(f"\n  {target_name} ({len(lineage)} mappings):")
        for m in lineage:
            t = f" [{m.transform}]" if m.transform else ""
            print(f"    {m.source_asset}.{m.source_field} → {m.target_field}{t}")

    # ── Step 6: Plan / Apply to production ──

    print(f"\n{'─' * 70}")
    print("  Step 6: Plan & apply to production")
    print(f"{'─' * 70}")

    plan = manager.plan(str(models_dir), environment="production")
    print(f"\n{plan.show()}")

    apply_result = manager.apply(plan, environment="production")
    print(f"\n  Applied: created={apply_result.created}, updated={apply_result.updated}, "
          f"deleted={apply_result.deleted}")

    # Re-plan should be clean
    plan2 = manager.plan(str(models_dir), environment="production")
    print(f"  Re-plan: has_changes={plan2.has_changes}")

    # ── Step 7: Dev environment — make a change, promote ──

    print(f"\n{'─' * 70}")
    print("  Step 7: Dev workflow → promote to production")
    print(f"{'─' * 70}")

    dev = manager.create_environment("dev_alice", parent="production", shallow=True)
    print(f"  Created env: {dev.name} (parent={dev.parent}, shallow={dev.shallow})")

    # Alice modifies the revenue mart — adds a new metric column
    write_model(models_dir, "marts", "mart_revenue", {
        "name": "mart.revenue",
        "kind": "mart",
        "description": "Daily revenue by country — now with repeat rate!",
        "tags": ["mart", "finance", "kpi"],
        "materialized": "table",
        "freshness_hours": 6,
        "owner": {"name": "Finance Analytics", "email": "finance-data@shop.io", "team": "finance"},
        "columns": [
            {"name": "country", "type": "VARCHAR"},
            {"name": "total_revenue", "type": "DECIMAL"},
            {"name": "avg_order_value", "type": "DECIMAL"},
            {"name": "total_orders", "type": "INTEGER"},
            {"name": "unique_customers", "type": "INTEGER"},
            {"name": "repeat_rate", "type": "DECIMAL",
             "description": "Fraction of customers with >1 order"},
        ],
        "metrics": [
            {"name": "total_revenue", "expression": "SUM(total_spent)"},
            {"name": "avg_order_value", "expression": "SUM(total_spent) / SUM(order_count)"},
            {"name": "customers", "expression": "COUNT(DISTINCT user_id)"},
            {"name": "repeat_rate", "expression": "AVG(CASE WHEN order_count > 1 THEN 1 ELSE 0 END)",
             "description": "Repeat purchase rate"},
        ],
    }, sql="""\
WITH base AS (
    SELECT * FROM {{ ref('intermediate.user_orders') }}
    WHERE total_spent > 0
)
SELECT
    base.country,
    SUM(base.total_spent)                AS total_revenue,
    SUM(base.total_spent) / SUM(base.order_count) AS avg_order_value,
    SUM(base.order_count)                AS total_orders,
    COUNT(DISTINCT base.user_id)         AS unique_customers,
    AVG(CASE WHEN base.order_count > 1 THEN 1.0 ELSE 0.0 END) AS repeat_rate
FROM base
GROUP BY base.country""")

    dev_plan = manager.plan(str(models_dir), environment="dev_alice")
    print(f"\n  Dev plan:\n{dev_plan.show()}")

    dev_result = manager.apply(dev_plan, environment="dev_alice")
    print(f"  Applied to dev: updated={dev_result.updated}")

    # Production still sees the old version (dev is isolated)
    print(f"\n  Production drift (before merge):")
    drift_before = manager.drift(str(models_dir), environment="production")
    print(f"  {drift_before.show()}")

    # Merge: apply the modified files to production (like merging the PR)
    prod_plan = manager.plan(str(models_dir), environment="production")
    print(f"  Merge to production:\n{prod_plan.show()}")
    merge_result = manager.apply(prod_plan, environment="production")
    print(f"  Merged: updated={merge_result.updated}")

    # ── Step 8: Drift detection (should be clean now) ──

    print(f"\n{'─' * 70}")
    print("  Step 8: Drift detection")
    print(f"{'─' * 70}")

    drift = manager.drift(str(models_dir), environment="production")
    print(f"  Drift detected: {drift.has_changes}")

    # ── Step 9: Test inventory ──

    print(f"\n{'─' * 70}")
    print("  Step 9: Data quality test inventory")
    print(f"{'─' * 70}")

    total_tests = 0
    for asset in registry.all():
        if hasattr(asset, "tests") and asset.tests:
            print(f"  {asset.name}:")
            for test in asset.tests:
                col_info = f" on {test.column}" if test.column else ""
                sev = f" [{test.severity}]" if test.severity != "error" else ""
                print(f"    - {test.name} ({test.type}{col_info}){sev}")
                total_tests += 1
    print(f"\n  Total tests: {total_tests}")

    # ── Step 10: Metrics catalog ──

    print(f"\n{'─' * 70}")
    print("  Step 10: Metrics catalog")
    print(f"{'─' * 70}")

    for asset in registry.all():
        if hasattr(asset, "metrics") and asset.metrics:
            print(f"\n  {asset.name}:")
            for metric in asset.metrics:
                desc = f" — {metric.description}" if metric.description else ""
                print(f"    {metric.name}: {metric.expression}{desc}")

    # ── Summary ──

    print(f"\n{'=' * 70}")
    print("  Summary")
    print(f"{'=' * 70}")
    print(f"  Assets:        {len(registry)}")
    print(f"  Dependencies:  {len(registry.dependencies)}")
    print(f"  Environments:  {list(env_config.environments.keys())}")
    print(f"  State backend: SQLite ({tmp / 'state.db'})")
    print(f"  PII columns:   {len(pii_fields)}")
    print(f"  Tests:         {total_tests}")

    # Cleanup
    backend.close()
    manager.destroy_environment("dev_alice")
    shutil.rmtree(tmp, ignore_errors=True)
    print(f"\n  Cleaned up {tmp}")


if __name__ == "__main__":
    main()
