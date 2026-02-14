#!/usr/bin/env python3
"""Demo 6: Real-World E-Commerce Analytics Platform.

A complete example showing how a consumer builds a data platform on top
of the assets library.  Covers:

  - Rich Pydantic models (Column as nested Asset with
    PII/classification, Metric, Owner, Tests)
  - Consumer-driven YAML + SQL loading (dbt-style)
  - sqlglot-based column lineage
  - Plan / apply / drift detection
  - Multi-environment promotion (dev -> staging -> production)
  - SQLite state backend

Run:
    pip install pyyaml sqlglot
    python demos/06_ecommerce_platform/main.py
"""

from __future__ import annotations

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

import re

from pydantic import BaseModel

from assets import (
    Asset,
    AssetField,
    DependencyResolver,
    Environment,
    EnvironmentConfig,
    FieldMapping,
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


class Column(Asset):
    """A typed column with optional PII flag and classification.

    Columns are nested assets — they live inside their parent asset's
    ``children`` list and can participate in lineage at any depth.
    """

    type: str = "VARCHAR"
    pii: bool = False
    classification: Classification = Classification.INTERNAL


Column.model_rebuild()


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

    Extends the base Asset with metrics, tests, owner info,
    freshness SLAs, and materialization strategy. Columns are
    stored as nested Asset children (inherited ``children`` field).
    """

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
# 2. Consumer-Driven YAML + SQL Loading (dbt-style project layout)
# ═══════════════════════════════════════════════════════════════


_REF_PATTERN = re.compile(r"\{\{\s*ref\(\s*['\"]([^'\"]+)['\"]\s*\)\s*\}\}")


def _resolve_refs(sql: str) -> tuple[str, list[str]]:
    """Resolve {{ ref('x') }} templates in SQL.

    Returns (clean_sql, depends_on_list).
    Consumer owns this — the library never parses SQL.
    """
    refs = list(dict.fromkeys(_REF_PATTERN.findall(sql)))
    clean = _REF_PATTERN.sub(lambda m: m.group(1), sql)
    return clean, refs


def load_project(registry: Registry, models_dir: Path) -> int:
    """Discover YAML files, merge companion SQL, register assets.

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

    Returns the number of assets loaded.
    """
    loaded = 0
    for path in sorted(models_dir.rglob("*.yaml")):
        data = yaml.safe_load(path.read_text())
        if not isinstance(data, dict):
            continue
        # Merge companion .sql file if it exists
        sql_path = path.with_suffix(".sql")
        if sql_path.exists():
            raw_sql = sql_path.read_text().strip()
            clean_sql, refs = _resolve_refs(raw_sql)
            data["sql"] = clean_sql
            if refs:
                data.setdefault("depends_on", refs)
        asset = DataModel.model_validate(data)
        registry.register(asset)
        loaded += 1
    return loaded


# ═══════════════════════════════════════════════════════════════
# 3. sqlglot-Based Column Dependency Resolver
# ═══════════════════════════════════════════════════════════════


class SqlglotDependencyResolver(DependencyResolver):
    """Production-quality dependency resolver using sqlglot.lineage().

    Traces columns through CTEs, subqueries, and JOINs.
    Falls back to regex for SQL that sqlglot can't parse.
    """

    def resolve(self, sql: str, schema: dict[str, list[str]]) -> list[FieldMapping]:
        if not HAS_SQLGLOT:
            return []

        mappings: list[FieldMapping] = []

        # Build schema in sqlglot format: {table: {col: type}}
        sg_schema: dict[str, dict[str, str]] = {}
        for table, children in schema.items():
            sg_schema[table] = {col: "VARCHAR" for col in children}

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
                    qualify_tables(parsed, schema=sg_schema),
                    schema=sg_schema,
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
                        source=f"{src_table}/{src_col}",
                        target=f"<target>/{target_col}",
                        transform=transform,
                    )
                )
        else:
            for child in node.downstream:
                self._walk_lineage(child, target_col, mappings)


# ═══════════════════════════════════════════════════════════════
# 4. Create the project on disk
# ═══════════════════════════════════════════════════════════════


def write_model(
    base: Path, subdir: str, name: str, data: dict, sql: str | None = None
) -> None:
    folder = base / subdir
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{name}.yaml").write_text(
        yaml.dump(data, default_flow_style=False, sort_keys=False)
    )
    if sql:
        (folder / f"{name}.sql").write_text(sql)


def create_project(root: Path) -> Path:
    models = root / "models"

    # ── Sources ──

    write_model(
        models,
        "sources",
        "raw_users",
        {
            "id": "raw.users",
            "type": "source",
            "description": "Raw users from production Postgres replica",
            "tags": ["raw", "pii", "users"],
            "materialized": "table",
            "freshness_hours": 1,
            "owner": {
                "name": "Data Platform",
                "email": "platform@shop.io",
                "team": "infrastructure",
            },
            "children": [
                {
                    "id": "user_id",
                    "type": "column",
                    "type": "INTEGER",
                    "description": "Primary key",
                },
                {
                    "id": "email",
                    "type": "column",
                    "type": "VARCHAR",
                    "pii": True,
                    "classification": "restricted",
                    "description": "User email address",
                },
                {
                    "id": "full_name",
                    "type": "column",
                    "type": "VARCHAR",
                    "pii": True,
                    "classification": "confidential",
                },
                {"id": "country", "type": "column", "type": "VARCHAR"},
                {
                    "id": "created_at",
                    "type": "column",
                    "type": "TIMESTAMP",
                    "description": "Account creation time",
                },
            ],
            "tests": [
                {"name": "user_id_not_null", "type": "not_null", "column": "user_id"},
                {"name": "user_id_unique", "type": "unique", "column": "user_id"},
                {"name": "email_not_null", "type": "not_null", "column": "email"},
            ],
        },
    )

    write_model(
        models,
        "sources",
        "raw_orders",
        {
            "id": "raw.orders",
            "type": "source",
            "description": "Raw orders from the transactional database",
            "tags": ["raw", "finance", "orders"],
            "materialized": "table",
            "freshness_hours": 1,
            "owner": {
                "name": "Data Platform",
                "email": "platform@shop.io",
                "team": "infrastructure",
            },
            "children": [
                {
                    "id": "order_id",
                    "type": "column",
                    "type": "INTEGER",
                    "description": "Primary key",
                },
                {
                    "id": "user_id",
                    "type": "column",
                    "type": "INTEGER",
                    "description": "FK to users",
                },
                {
                    "id": "amount",
                    "type": "column",
                    "type": "DECIMAL",
                    "description": "Order total in USD",
                },
                {"id": "currency", "type": "column", "type": "VARCHAR"},
                {
                    "id": "status",
                    "type": "column",
                    "type": "VARCHAR",
                    "description": "pending/completed/refunded",
                },
                {"id": "ordered_at", "type": "column", "type": "TIMESTAMP"},
            ],
            "tests": [
                {"name": "order_id_not_null", "type": "not_null", "column": "order_id"},
                {
                    "name": "amount_positive",
                    "type": "custom_sql",
                    "column": "amount",
                    "config": {
                        "sql": "SELECT * FROM {{ ref('raw.orders') }} WHERE amount < 0"
                    },
                },
            ],
        },
    )

    write_model(
        models,
        "sources",
        "raw_products",
        {
            "id": "raw.products",
            "type": "source",
            "description": "Product catalog from the CMS",
            "tags": ["raw", "catalog"],
            "materialized": "table",
            "children": [
                {"id": "product_id", "type": "column", "type": "INTEGER"},
                {"id": "name", "type": "column", "type": "VARCHAR"},
                {"id": "category", "type": "column", "type": "VARCHAR"},
                {"id": "price", "type": "column", "type": "DECIMAL"},
                {"id": "is_active", "type": "column", "type": "BOOLEAN"},
            ],
        },
    )

    # ── Staging ──

    write_model(
        models,
        "staging",
        "stg_users",
        {
            "id": "staging.users",
            "type": "staging",
            "description": "Cleaned and validated user records — PII handled",
            "tags": ["staging", "pii", "users"],
            "materialized": "view",
            "owner": {"name": "Analytics Engineering", "team": "analytics"},
            "children": [
                {"id": "user_id", "type": "column", "type": "INTEGER"},
                {
                    "id": "email_domain",
                    "type": "column",
                    "type": "VARCHAR",
                    "description": "Domain part of email (PII-safe)",
                },
                {"id": "country", "type": "column", "type": "VARCHAR"},
                {"id": "created_at", "type": "column", "type": "TIMESTAMP"},
            ],
            "tests": [
                {"name": "user_id_not_null", "type": "not_null", "column": "user_id"},
            ],
        },
        sql="""\
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
SELECT * FROM cleaned""",
    )

    write_model(
        models,
        "staging",
        "stg_orders",
        {
            "id": "staging.orders",
            "type": "staging",
            "description": "Validated orders — only completed, amounts > 0",
            "tags": ["staging", "finance", "orders"],
            "materialized": "view",
            "owner": {"name": "Analytics Engineering", "team": "analytics"},
            "children": [
                {"id": "order_id", "type": "column", "type": "INTEGER"},
                {"id": "user_id", "type": "column", "type": "INTEGER"},
                {
                    "id": "amount_usd",
                    "type": "column",
                    "type": "DECIMAL",
                    "description": "Amount normalized to USD",
                },
                {"id": "ordered_at", "type": "column", "type": "TIMESTAMP"},
            ],
            "tests": [
                {"name": "order_id_not_null", "type": "not_null", "column": "order_id"},
                {
                    "name": "amount_positive",
                    "type": "custom_sql",
                    "column": "amount_usd",
                    "severity": "warn",
                },
            ],
        },
        sql="""\
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
SELECT * FROM normalized""",
    )

    # ── Intermediate ──

    write_model(
        models,
        "intermediate",
        "int_user_orders",
        {
            "id": "intermediate.user_orders",
            "type": "intermediate",
            "description": "User enriched with order aggregates",
            "tags": ["intermediate", "users", "orders"],
            "materialized": "view",
            "children": [
                {"id": "user_id", "type": "column", "type": "INTEGER"},
                {"id": "email_domain", "type": "column", "type": "VARCHAR"},
                {"id": "country", "type": "column", "type": "VARCHAR"},
                {"id": "total_spent", "type": "column", "type": "DECIMAL"},
                {"id": "order_count", "type": "column", "type": "INTEGER"},
                {"id": "first_order_at", "type": "column", "type": "TIMESTAMP"},
                {"id": "last_order_at", "type": "column", "type": "TIMESTAMP"},
            ],
        },
        sql="""\
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
LEFT JOIN orders ON users.user_id = orders.user_id""",
    )

    # ── Marts ──

    write_model(
        models,
        "marts",
        "mart_revenue",
        {
            "id": "mart.revenue",
            "type": "mart",
            "description": "Daily revenue by country — the core finance KPI model",
            "tags": ["mart", "finance", "kpi"],
            "materialized": "table",
            "freshness_hours": 6,
            "owner": {
                "name": "Finance Analytics",
                "email": "finance-data@shop.io",
                "team": "finance",
            },
            "children": [
                {"id": "country", "type": "column", "type": "VARCHAR"},
                {"id": "total_revenue", "type": "column", "type": "DECIMAL"},
                {"id": "avg_order_value", "type": "column", "type": "DECIMAL"},
                {"id": "total_orders", "type": "column", "type": "INTEGER"},
                {"id": "unique_customers", "type": "column", "type": "INTEGER"},
            ],
            "metrics": [
                {
                    "name": "total_revenue",
                    "expression": "SUM(total_spent)",
                    "description": "Gross revenue across all customers",
                },
                {
                    "name": "avg_order_value",
                    "expression": "SUM(total_spent) / SUM(order_count)",
                    "description": "Average value per order",
                },
                {"name": "customers", "expression": "COUNT(DISTINCT user_id)"},
            ],
            "tests": [
                {
                    "name": "revenue_not_negative",
                    "type": "custom_sql",
                    "config": {
                        "sql": (
                            "SELECT * FROM {{ ref('mart.revenue') }} "
                            "WHERE total_revenue < 0"
                        )
                    },
                },
            ],
        },
        sql="""\
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
GROUP BY base.country""",
    )

    write_model(
        models,
        "marts",
        "mart_customer_segments",
        {
            "id": "mart.customer_segments",
            "type": "mart",
            "description": "Customer segmentation based on spending behavior",
            "tags": ["mart", "marketing", "users"],
            "materialized": "table",
            "owner": {"name": "Marketing Analytics", "team": "marketing"},
            "children": [
                {"id": "user_id", "type": "column", "type": "INTEGER"},
                {"id": "email_domain", "type": "column", "type": "VARCHAR"},
                {"id": "country", "type": "column", "type": "VARCHAR"},
                {
                    "id": "segment",
                    "type": "column",
                    "type": "VARCHAR",
                    "description": "vip/regular/new/inactive",
                },
                {"id": "lifetime_value", "type": "column", "type": "DECIMAL"},
                {"id": "order_count", "type": "column", "type": "INTEGER"},
            ],
        },
        sql="""\
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
SELECT * FROM segmented""",
    )

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
    manager = StateManager(registry, backend, env_config)

    # ── Step 1: Load the project ──

    print(f"\n{'─' * 70}")
    print("  Step 1: Load project")
    print(f"{'─' * 70}")

    loaded = load_project(registry, models_dir)
    print(f"  Loaded {loaded} assets")

    print(f"\n  Registered assets ({len(registry)}):")
    for asset in registry.all():
        n_children = len(asset.children)
        tests = len(asset.tests) if hasattr(asset, "tests") else 0
        deps = f"  deps={asset.depends_on}" if asset.depends_on else ""
        print(
            f"    {asset.id:<35} type={asset.type:<15} "
            f"children={n_children}  tests={tests}{deps}"
        )

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

    for selector in [
        "tag:pii",
        "type:mart",
        "raw.*",
        "+mart.revenue",
        "tag:finance,type:mart",
    ]:
        result_sel = registry.select(selector)
        print(f"  {selector:<30} → {sorted(result_sel.names)}")

    # ── Step 4: Nested asset introspection ──

    print(f"\n{'─' * 70}")
    print("  Step 4: Nested asset introspection")
    print(f"{'─' * 70}")

    stg_users = registry.get("staging.users")
    print(f"  staging.users children: {stg_users.list_children()}")

    email_col = stg_users.get_child("email_domain")
    print(f"  email_domain → type={email_col.type}, desc='{email_col.description}'")

    # PII scan across all models (inspecting children)
    pii_fields = []
    for asset in registry.all():
        for child in asset.children:
            if hasattr(child, "pii") and child.pii:
                pii_fields.append(f"{asset.id}/{child.id}")
    print(f"\n  PII columns across all models: {pii_fields}")

    # Restricted classification scan
    restricted = []
    for asset in registry.all():
        for child in asset.children:
            if (
                hasattr(child, "classification")
                and child.classification == Classification.RESTRICTED
            ):
                restricted.append(f"{asset.id}/{child.id}")
    print(f"  RESTRICTED columns: {restricted}")

    # ── Step 5: Column lineage with sqlglot ──

    print(f"\n{'─' * 70}")
    print("  Step 5: Column-level lineage (sqlglot)")
    print(f"{'─' * 70}")

    resolver = SqlglotDependencyResolver()

    for target_name in ["staging.users", "intermediate.user_orders", "mart.revenue"]:
        lineage = registry.resolve_field_dependency(
            asset_id=target_name, resolver=resolver
        )
        print(f"\n  {target_name} ({len(lineage)} mappings):")
        for m in lineage:
            t = f" [{m.transform}]" if m.transform else ""
            print(f"    {m.source_asset}/{m.source_field} → {m.target_field}{t}")

    # ── Step 6: Plan / Apply to production ──

    print(f"\n{'─' * 70}")
    print("  Step 6: Plan & apply to production")
    print(f"{'─' * 70}")

    plan = manager.plan(environment="production")
    print(f"\n{plan.show()}")

    apply_result = manager.apply(plan, environment="production")
    print(
        f"\n  Applied: created={apply_result.created}, updated={apply_result.updated}, "
        f"deleted={apply_result.deleted}"
    )

    # Re-plan should be clean
    registry.clear()
    load_project(registry, models_dir)
    plan2 = manager.plan(environment="production")
    print(f"  Re-plan: has_changes={plan2.has_changes}")

    # ── Step 7: Dev environment — make a change, promote ──

    print(f"\n{'─' * 70}")
    print("  Step 7: Dev workflow → promote to production")
    print(f"{'─' * 70}")

    dev = manager.create_environment("dev_alice", parent="production", shallow=True)
    print(f"  Created env: {dev.name} (parent={dev.parent}, shallow={dev.shallow})")

    # Alice modifies the revenue mart — adds a new metric column
    write_model(
        models_dir,
        "marts",
        "mart_revenue",
        {
            "id": "mart.revenue",
            "type": "mart",
            "description": "Daily revenue by country — now with repeat rate!",
            "tags": ["mart", "finance", "kpi"],
            "materialized": "table",
            "freshness_hours": 6,
            "owner": {
                "name": "Finance Analytics",
                "email": "finance-data@shop.io",
                "team": "finance",
            },
            "children": [
                {"id": "country", "type": "column", "type": "VARCHAR"},
                {"id": "total_revenue", "type": "column", "type": "DECIMAL"},
                {"id": "avg_order_value", "type": "column", "type": "DECIMAL"},
                {"id": "total_orders", "type": "column", "type": "INTEGER"},
                {"id": "unique_customers", "type": "column", "type": "INTEGER"},
                {
                    "id": "repeat_rate",
                    "type": "column",
                    "type": "DECIMAL",
                    "description": "Fraction of customers with >1 order",
                },
            ],
            "metrics": [
                {"name": "total_revenue", "expression": "SUM(total_spent)"},
                {
                    "name": "avg_order_value",
                    "expression": "SUM(total_spent) / SUM(order_count)",
                },
                {"name": "customers", "expression": "COUNT(DISTINCT user_id)"},
                {
                    "name": "repeat_rate",
                    "expression": "AVG(CASE WHEN order_count > 1 THEN 1 ELSE 0 END)",
                    "description": "Repeat purchase rate",
                },
            ],
        },
        sql="""\
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
GROUP BY base.country""",
    )

    registry.clear()
    load_project(registry, models_dir)
    dev_plan = manager.plan(environment="dev_alice")
    print(f"\n  Dev plan:\n{dev_plan.show()}")

    dev_result = manager.apply(dev_plan, environment="dev_alice")
    print(f"  Applied to dev: updated={dev_result.updated}")

    # Production still sees the old version (dev is isolated)
    print("\n  Production drift (before merge):")
    registry.clear()
    load_project(registry, models_dir)
    drift_before = manager.drift(environment="production")
    print(f"  {drift_before.show()}")

    # Merge: apply the modified files to production (like merging the PR)
    registry.clear()
    load_project(registry, models_dir)
    prod_plan = manager.plan(environment="production")
    print(f"  Merge to production:\n{prod_plan.show()}")
    merge_result = manager.apply(prod_plan, environment="production")
    print(f"  Merged: updated={merge_result.updated}")

    # ── Step 8: Drift detection (should be clean now) ──

    print(f"\n{'─' * 70}")
    print("  Step 8: Drift detection")
    print(f"{'─' * 70}")

    registry.clear()
    load_project(registry, models_dir)
    drift = manager.drift(environment="production")
    print(f"  Drift detected: {drift.has_changes}")

    # ── Step 9: Test inventory ──

    print(f"\n{'─' * 70}")
    print("  Step 9: Data quality test inventory")
    print(f"{'─' * 70}")

    total_tests = 0
    for asset in registry.all():
        if hasattr(asset, "tests") and asset.tests:
            print(f"  {asset.id}:")
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
            print(f"\n  {asset.id}:")
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
