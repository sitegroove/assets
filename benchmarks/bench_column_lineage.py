"""Benchmark — Column lineage with sqlglot over 500 models in SQLite.

Run:
    python benchmarks/bench_column_lineage.py

Generates a realistic 500-model DAG (sources → staging → intermediate → marts → reports),
parses SQL with sqlglot to extract column-level lineage, merges discovered columns with
manually-defined column descriptions, stores everything in SQLite, and benchmarks
SELECT queries from simple single-model lookups to complex downstream lineage traversals.
"""

from __future__ import annotations

import hashlib
import json
import random
import shutil
import sqlite3
import statistics
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

# Ensure the project root is importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import sqlglot

from assets import SQLiteBackend
from assets.state.models import AssetState, DependencyState, StateSnapshot

# ── Configuration ────────────────────────────────────────────
NUM_SOURCES = 50
NUM_STAGING = 100
NUM_INTERMEDIATE = 150
NUM_MARTS = 150
NUM_REPORTS = 50
NUM_ASSETS = NUM_SOURCES + NUM_STAGING + NUM_INTERMEDIATE + NUM_MARTS + NUM_REPORTS  # 500
ITERATIONS = 5
ENVIRONMENT = "production"
SEED = 42

# ── Column pools (realistic column names) ────────────────────
_ID_COLS = ["id", "uuid", "external_id"]
_TIMESTAMP_COLS = ["created_at", "updated_at", "deleted_at", "event_ts"]
_USER_COLS = ["user_id", "username", "email", "first_name", "last_name", "phone", "country"]
_ORDER_COLS = ["order_id", "amount", "currency", "status", "discount", "tax", "total"]
_PRODUCT_COLS = ["product_id", "product_name", "category", "price", "sku", "brand"]
_METRIC_COLS = ["count", "total_amount", "avg_amount", "min_amount", "max_amount"]
_DIM_COLS = ["region", "segment", "channel", "platform", "device_type"]

ALL_COL_POOLS = [_ID_COLS, _TIMESTAMP_COLS, _USER_COLS, _ORDER_COLS,
                 _PRODUCT_COLS, _METRIC_COLS, _DIM_COLS]

# Column type mapping
COL_TYPES = {
    "id": "INT", "uuid": "VARCHAR", "external_id": "VARCHAR",
    "created_at": "TIMESTAMP", "updated_at": "TIMESTAMP",
    "deleted_at": "TIMESTAMP", "event_ts": "TIMESTAMP",
    "user_id": "INT", "username": "VARCHAR", "email": "VARCHAR",
    "first_name": "VARCHAR", "last_name": "VARCHAR", "phone": "VARCHAR",
    "country": "VARCHAR",
    "order_id": "INT", "amount": "DECIMAL", "currency": "VARCHAR",
    "status": "VARCHAR", "discount": "DECIMAL", "tax": "DECIMAL",
    "total": "DECIMAL",
    "product_id": "INT", "product_name": "VARCHAR", "category": "VARCHAR",
    "price": "DECIMAL", "sku": "VARCHAR", "brand": "VARCHAR",
    "count": "INT", "total_amount": "DECIMAL", "avg_amount": "DECIMAL",
    "min_amount": "DECIMAL", "max_amount": "DECIMAL",
    "region": "VARCHAR", "segment": "VARCHAR", "channel": "VARCHAR",
    "platform": "VARCHAR", "device_type": "VARCHAR",
}


# ── Column description templates ─────────────────────────────
COL_DESCRIPTIONS = {
    "id": "Primary key identifier",
    "uuid": "Universally unique identifier",
    "user_id": "Foreign key to the users table",
    "email": "User email address",
    "amount": "Transaction amount in base currency",
    "total_amount": "Aggregated total amount",
    "avg_amount": "Average transaction amount",
    "status": "Current status of the record",
    "created_at": "Timestamp when the record was created",
    "updated_at": "Timestamp of last modification",
    "category": "Product category classification",
    "region": "Geographic region",
    "count": "Number of records",
}


# ── Model definitions ────────────────────────────────────────

class ModelDef:
    """Holds a generated model definition before it goes into SQLite."""

    __slots__ = ("name", "layer", "description", "sql", "depends_on",
                 "defined_columns", "all_columns", "column_lineage")

    def __init__(
        self,
        name: str,
        layer: str,
        description: str,
        sql: str | None,
        depends_on: list[str],
        defined_columns: dict[str, dict[str, str]],
    ):
        self.name = name
        self.layer = layer
        self.description = description
        self.sql = sql
        self.depends_on = depends_on
        # Columns explicitly defined with descriptions: {col_name: {type, description}}
        self.defined_columns = defined_columns
        # Filled after sqlglot parsing — full list including discovered columns
        self.all_columns: dict[str, dict[str, str]] = {}
        # Filled after lineage extraction: [(src_model, src_col, tgt_col, transform)]
        self.column_lineage: list[tuple[str, str, str, str | None]] = []


def _pick_columns(rng: random.Random, n: int) -> list[str]:
    """Pick n distinct column names from the pools."""
    pool: list[str] = []
    for p in ALL_COL_POOLS:
        pool.extend(p)
    chosen = rng.sample(pool, min(n, len(pool)))
    # Always include 'id' for join-ability
    if "id" not in chosen:
        chosen[0] = "id"
    return chosen


def _make_col_defs(
    cols: list[str],
    rng: random.Random,
    describe_fraction: float = 0.7,
    include_fraction: float = 1.0,
) -> dict[str, dict[str, str]]:
    """Create column definitions; only include/describe a fraction to test merging.

    Args:
        cols: All output column names.
        describe_fraction: Fraction of included columns that get a description.
        include_fraction: Fraction of columns to include in the definition at all.
            Columns not included will be "discovered" by sqlglot later.
    """
    defs: dict[str, dict[str, str]] = {}
    for c in cols:
        if rng.random() > include_fraction:
            continue  # skip — sqlglot will discover this column
        entry: dict[str, str] = {"type": COL_TYPES.get(c, "VARCHAR")}
        if rng.random() < describe_fraction and c in COL_DESCRIPTIONS:
            entry["description"] = COL_DESCRIPTIONS[c]
        defs[c] = entry
    return defs


def generate_dag(seed: int = SEED) -> tuple[list[ModelDef], dict[str, list[str]]]:
    """Build a 500-model DAG and return (models, schema_for_sqlglot)."""
    rng = random.Random(seed)
    models: list[ModelDef] = []
    # schema: {table_name: [col_names]} for sqlglot
    schema: dict[str, dict[str, str]] = {}
    models_by_name: dict[str, ModelDef] = {}

    # ── Layer 0: Sources (no SQL, raw tables) ──
    for i in range(NUM_SOURCES):
        name = f"raw_{i:03d}"
        cols = _pick_columns(rng, rng.randint(5, 12))
        col_defs = _make_col_defs(cols, rng, describe_fraction=0.9)
        m = ModelDef(
            name=name, layer="source",
            description=f"Raw source table {i} ingested from upstream system",
            sql=None, depends_on=[], defined_columns=col_defs,
        )
        m.all_columns = dict(col_defs)  # sources have no SQL to parse
        models.append(m)
        models_by_name[name] = m
        schema[name] = {c: col_defs[c]["type"] for c in col_defs}

    source_names = [m.name for m in models if m.layer == "source"]

    # ── Layer 1: Staging (simple SELECT FROM one source, with alias) ──
    for i in range(NUM_STAGING):
        name = f"stg_{i:03d}"
        src = rng.choice(source_names)
        src_cols = list(schema[src].keys())
        selected = rng.sample(src_cols, min(rng.randint(3, len(src_cols)), len(src_cols)))
        select_parts = []
        for c in selected:
            if rng.random() < 0.15 and c not in ("id",):
                alias = f"{c}_cleaned"
                select_parts.append(f"COALESCE(s.{c}, '') AS {alias}")
            else:
                select_parts.append(f"s.{c}")
        sql = f"SELECT {', '.join(select_parts)} FROM {src} s"
        output_cols = []
        for part in select_parts:
            if " AS " in part:
                output_cols.append(part.split(" AS ")[-1].strip())
            else:
                output_cols.append(part.split(".")[-1].strip())
        # Only define ~60% of columns; rest will be discovered by sqlglot
        col_defs = _make_col_defs(output_cols, rng, describe_fraction=0.6, include_fraction=0.6)
        m = ModelDef(
            name=name, layer="staging", depends_on=[src],
            description=f"Cleaned staging model from {src}",
            sql=sql, defined_columns=col_defs,
        )
        models.append(m)
        models_by_name[name] = m

    # ── Layer 2: Intermediate (JOIN 2-3 staging models) ──
    staging_names = [m.name for m in models if m.layer == "staging"]
    for i in range(NUM_INTERMEDIATE):
        name = f"int_{i:03d}"
        n_deps = rng.randint(2, min(3, len(staging_names)))
        deps = rng.sample(staging_names, n_deps)
        # Build JOIN SQL
        base = deps[0]
        base_alias = "a"
        select_parts = [f"{base_alias}.id"]
        from_clause = f"{base} {base_alias}"
        for j, dep in enumerate(deps[1:], start=1):
            alias = chr(ord("b") + j - 1)
            from_clause += f" JOIN {dep} {alias} ON {base_alias}.id = {alias}.id"
            dep_cols = list(schema.get(dep, {}).keys()) if dep in schema else ["id"]
            for c in dep_cols[:3]:
                if c != "id":
                    select_parts.append(f"{alias}.{c}")
        # Add a few columns from base
        base_cols = list(schema.get(base, {}).keys()) if base in schema else []
        for c in base_cols[:4]:
            if c != "id":
                select_parts.append(f"{base_alias}.{c}")
        sql = f"SELECT {', '.join(select_parts)} FROM {from_clause}"
        output_cols = [p.split(".")[-1].split(" AS ")[-1].strip() for p in select_parts]
        # Deduplicate
        seen: set[str] = set()
        unique_cols: list[str] = []
        for c in output_cols:
            if c not in seen:
                seen.add(c)
                unique_cols.append(c)
        col_defs = _make_col_defs(unique_cols, rng, describe_fraction=0.5, include_fraction=0.5)
        m = ModelDef(
            name=name, layer="intermediate", depends_on=deps,
            description=f"Intermediate model joining {', '.join(deps)}",
            sql=sql, defined_columns=col_defs,
        )
        models.append(m)
        models_by_name[name] = m

    # ── Layer 3: Marts (aggregate from intermediate, maybe join with staging) ──
    int_names = [m.name for m in models if m.layer == "intermediate"]
    for i in range(NUM_MARTS):
        name = f"mart_{i:03d}"
        base = rng.choice(int_names)
        deps = [base]
        # Sometimes join with a staging model
        extra_join = ""
        if rng.random() < 0.4 and staging_names:
            extra = rng.choice(staging_names)
            deps.append(extra)
            extra_join = f" LEFT JOIN {extra} s ON a.id = s.id"
        base_cols = list(schema.get(base, {}).keys()) if base in schema else ["id"]
        group_cols = [c for c in base_cols if c in ("id", "region", "segment",
                      "category", "channel", "country", "status")][:2]
        if not group_cols:
            group_cols = ["id"]
        agg_cols = [c for c in base_cols if c in ("amount", "total", "price",
                    "count", "tax", "discount")][:2]
        select_parts = [f"a.{c}" for c in group_cols]
        for c in agg_cols:
            select_parts.append(f"SUM(a.{c}) AS total_{c}")
            select_parts.append(f"AVG(a.{c}) AS avg_{c}")
        select_parts.append("COUNT(*) AS record_count")
        sql = (
            f"SELECT {', '.join(select_parts)} FROM {base} a{extra_join} "
            f"GROUP BY {', '.join(f'a.{c}' for c in group_cols)}"
        )
        output_cols = []
        for p in select_parts:
            if " AS " in p:
                output_cols.append(p.split(" AS ")[-1].strip())
            else:
                output_cols.append(p.split(".")[-1].strip())
        col_defs = _make_col_defs(output_cols, rng, describe_fraction=0.4, include_fraction=0.5)
        m = ModelDef(
            name=name, layer="mart", depends_on=deps,
            description=f"Mart aggregation model from {base}",
            sql=sql, defined_columns=col_defs,
        )
        models.append(m)
        models_by_name[name] = m

    # ── Layer 4: Reports (SELECT from marts with UNION or simple transforms) ──
    mart_names = [m.name for m in models if m.layer == "mart"]
    for i in range(NUM_REPORTS):
        name = f"report_{i:03d}"
        if rng.random() < 0.5 and len(mart_names) >= 2:
            # UNION of two marts — each side aliased
            m1, m2 = rng.sample(mart_names, 2)
            deps = [m1, m2]
            m1_cols = list(schema.get(m1, {}).keys()) if m1 in schema else ["id"]
            shared = m1_cols[:3]
            if not shared:
                shared = ["id"]
            sel_a = ", ".join(f"a.{c}" for c in shared)
            sel_b = ", ".join(f"b.{c}" for c in shared)
            sql = f"SELECT {sel_a} FROM {m1} a UNION ALL SELECT {sel_b} FROM {m2} b"
        else:
            # Simple select from one mart with alias
            src = rng.choice(mart_names)
            deps = [src]
            src_cols = list(schema.get(src, {}).keys()) if src in schema else ["id"]
            cols = src_cols[:5] if src_cols else ["id"]
            sel = ", ".join(f"r.{c}" for c in cols)
            sql = f"SELECT {sel} FROM {src} r"
        # Extract output column names from SQL
        parsed_tmp = sqlglot.parse_one(sql)
        output_cols_raw = [s.alias_or_name for s in parsed_tmp.selects if s.alias_or_name]
        if not output_cols_raw:
            output_cols_raw = ["id"]
        col_defs = _make_col_defs(output_cols_raw, rng, describe_fraction=0.3, include_fraction=0.4)
        m = ModelDef(
            name=name, layer="report", depends_on=deps,
            description=f"Report model for executive dashboards",
            sql=sql, defined_columns=col_defs,
        )
        models.append(m)
        models_by_name[name] = m

    # Build schema dict progressively (sources already in schema)
    # We need to resolve schemas in topological order for sqlglot
    for m in models:
        if m.layer == "source":
            continue
        # Output columns from defined_columns as a starting schema
        schema[m.name] = {c: v.get("type", "VARCHAR") for c, v in m.defined_columns.items()}

    return models, schema


# ── Lineage extraction (fast AST-based approach) ─────────────

def extract_lineage_for_model(
    model: ModelDef,
    schema: dict[str, dict[str, str]],
) -> None:
    """Use sqlglot to parse SQL, extract output columns and column lineage.

    - Columns found by sqlglot but missing from model.defined_columns are added
      without descriptions (marked as "discovered").
    - Column lineage is stored as (src_model, src_col, tgt_col, transform).

    Uses direct AST inspection (parse_one → selects → Column nodes) instead of
    sqlglot.lineage() for ~100x faster extraction.
    """
    if model.sql is None:
        model.all_columns = dict(model.defined_columns)
        return

    # Start with defined columns
    model.all_columns = dict(model.defined_columns)
    model.column_lineage = []

    try:
        parsed = sqlglot.parse_one(model.sql)
    except Exception:
        return

    # Build alias → table name mapping from all Table nodes
    table_aliases: dict[str, str] = {}
    for table in parsed.find_all(sqlglot.exp.Table):
        tbl_name = table.name
        alias = table.alias or tbl_name
        table_aliases[alias] = tbl_name
        table_aliases[tbl_name] = tbl_name

    # Walk each SELECT expression to extract output columns and lineage
    for sel in parsed.selects:
        output_name = sel.alias_or_name
        if not output_name or output_name == "*":
            continue

        # Discover columns not in defined_columns
        if output_name not in model.all_columns:
            inferred_type = COL_TYPES.get(output_name, "VARCHAR")
            model.all_columns[output_name] = {"type": inferred_type}

        # Detect aggregate/transform function wrapping this expression
        transform: str | None = None
        sel_sql = sel.sql().upper()
        for fn in ("SUM", "AVG", "COUNT", "COALESCE", "MIN", "MAX"):
            if fn + "(" in sel_sql:
                transform = fn
                break

        # Trace source columns
        for col_ref in sel.find_all(sqlglot.exp.Column):
            src_alias = col_ref.table
            src_col = col_ref.name
            src_table = table_aliases.get(src_alias, src_alias)
            if src_table:
                model.column_lineage.append((src_table, src_col, output_name, transform))


# ── SQLite lineage schema ────────────────────────────────────

LINEAGE_SCHEMA = """\
CREATE TABLE IF NOT EXISTS models (
    name        TEXT PRIMARY KEY,
    layer       TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    sql_text    TEXT,
    fingerprint TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS model_dependencies (
    source TEXT NOT NULL,
    target TEXT NOT NULL,
    PRIMARY KEY (source, target)
);

CREATE TABLE IF NOT EXISTS columns (
    model_name  TEXT NOT NULL,
    column_name TEXT NOT NULL,
    data_type   TEXT NOT NULL DEFAULT 'VARCHAR',
    description TEXT NOT NULL DEFAULT '',
    source      TEXT NOT NULL DEFAULT 'defined',
    PRIMARY KEY (model_name, column_name)
);

CREATE TABLE IF NOT EXISTS column_lineage (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    source_model    TEXT NOT NULL,
    source_column   TEXT NOT NULL,
    target_model    TEXT NOT NULL,
    target_column   TEXT NOT NULL,
    transform       TEXT
);

CREATE INDEX IF NOT EXISTS idx_col_model        ON columns(model_name);
CREATE INDEX IF NOT EXISTS idx_lineage_target    ON column_lineage(target_model, target_column);
CREATE INDEX IF NOT EXISTS idx_lineage_source    ON column_lineage(source_model, source_column);
CREATE INDEX IF NOT EXISTS idx_deps_source       ON model_dependencies(source);
CREATE INDEX IF NOT EXISTS idx_deps_target       ON model_dependencies(target);
"""


def create_lineage_db(db_path: Path) -> sqlite3.Connection:
    """Create the lineage SQLite database with schema."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.executescript(LINEAGE_SCHEMA)
    return conn


def store_lineage(conn: sqlite3.Connection, models: list[ModelDef]) -> None:
    """Store all models, columns, and lineage into SQLite."""
    with conn:
        # Insert models
        conn.executemany(
            "INSERT OR REPLACE INTO models (name, layer, description, sql_text, fingerprint) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                (
                    m.name, m.layer, m.description, m.sql,
                    hashlib.sha256(f"{m.name}:{m.sql or ''}".encode()).hexdigest(),
                )
                for m in models
            ),
        )

        # Insert dependencies
        conn.executemany(
            "INSERT OR REPLACE INTO model_dependencies (source, target) VALUES (?, ?)",
            (
                (dep, m.name)
                for m in models
                for dep in m.depends_on
            ),
        )

        # Insert columns
        conn.executemany(
            "INSERT OR REPLACE INTO columns (model_name, column_name, data_type, description, source) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                (
                    m.name, col_name,
                    col_info.get("type", "VARCHAR"),
                    col_info.get("description", ""),
                    "defined" if col_name in m.defined_columns else "discovered",
                )
                for m in models
                for col_name, col_info in m.all_columns.items()
            ),
        )

        # Insert column lineage
        conn.executemany(
            "INSERT INTO column_lineage (source_model, source_column, target_model, target_column, transform) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                (src_model, src_col, m.name, tgt_col, transform)
                for m in models
                for src_model, src_col, tgt_col, transform in m.column_lineage
            ),
        )


# ── Benchmark helpers ────────────────────────────────────────

def _timed(fn, iterations: int = ITERATIONS) -> dict[str, float]:
    """Run fn() multiple times and return timing stats in milliseconds."""
    times: list[float] = []
    for _ in range(iterations):
        start = time.perf_counter()
        fn()
        elapsed = (time.perf_counter() - start) * 1000
        times.append(elapsed)
    return {
        "min_ms": min(times),
        "max_ms": max(times),
        "mean_ms": statistics.mean(times),
        "median_ms": statistics.median(times),
        "stdev_ms": statistics.stdev(times) if len(times) > 1 else 0.0,
    }


def _fmt(stats: dict[str, float]) -> str:
    return (
        f"  mean={stats['mean_ms']:8.2f}ms  "
        f"median={stats['median_ms']:8.2f}ms  "
        f"min={stats['min_ms']:8.2f}ms  "
        f"max={stats['max_ms']:8.2f}ms  "
        f"stdev={stats['stdev_ms']:7.2f}ms"
    )


# ── Query benchmarks ─────────────────────────────────────────

def bench_simple_queries(conn: sqlite3.Connection) -> dict[str, dict[str, float]]:
    """Benchmark simple SELECT queries."""
    print(f"\n{'─' * 70}")
    print("  Simple Queries")
    print(f"{'─' * 70}")
    results: dict[str, dict[str, float]] = {}

    # 1) Get all columns for a single model
    def q_columns_single():
        conn.execute(
            "SELECT column_name, data_type, description FROM columns WHERE model_name = ?",
            ("int_050",),
        ).fetchall()

    stats = _timed(q_columns_single)
    results["columns_single_model"] = stats
    row_count = len(conn.execute(
        "SELECT column_name FROM columns WHERE model_name = ?", ("int_050",)
    ).fetchall())
    print(f"  Columns for 1 model ({row_count} cols):       {_fmt(stats)}")

    # 2) Get all columns for 10 models
    def q_columns_batch():
        conn.execute(
            "SELECT model_name, column_name, data_type, description FROM columns "
            "WHERE model_name IN ('int_010','int_020','int_030','int_040','int_050',"
            "'stg_010','stg_020','mart_010','mart_020','report_010')",
        ).fetchall()

    stats = _timed(q_columns_batch)
    results["columns_10_models"] = stats
    row_count = len(conn.execute(
        "SELECT column_name FROM columns "
        "WHERE model_name IN ('int_010','int_020','int_030','int_040','int_050',"
        "'stg_010','stg_020','mart_010','mart_020','report_010')"
    ).fetchall())
    print(f"  Columns for 10 models ({row_count} cols):     {_fmt(stats)}")

    # 3) Get direct upstream lineage for one column
    def q_upstream_one():
        conn.execute(
            "SELECT source_model, source_column, transform "
            "FROM column_lineage WHERE target_model = ? AND target_column = ?",
            ("int_050", "id"),
        ).fetchall()

    stats = _timed(q_upstream_one)
    results["upstream_one_column"] = stats
    row_count = len(conn.execute(
        "SELECT source_model FROM column_lineage WHERE target_model = 'int_050' AND target_column = 'id'"
    ).fetchall())
    print(f"  Upstream lineage for 1 col ({row_count} rows):  {_fmt(stats)}")

    # 4) Get all columns across entire project
    def q_all_columns():
        conn.execute("SELECT model_name, column_name, data_type FROM columns").fetchall()

    stats = _timed(q_all_columns)
    results["all_columns"] = stats
    total = conn.execute("SELECT COUNT(*) FROM columns").fetchone()[0]
    print(f"  All columns in project ({total} total):   {_fmt(stats)}")

    # 5) Count columns per model
    def q_col_count_per_model():
        conn.execute(
            "SELECT model_name, COUNT(*) as col_count FROM columns "
            "GROUP BY model_name ORDER BY col_count DESC"
        ).fetchall()

    stats = _timed(q_col_count_per_model)
    results["col_count_per_model"] = stats
    print(f"  Column count per model (GROUP BY):      {_fmt(stats)}")

    # 6) Find all models with a specific column name
    def q_models_with_column():
        conn.execute(
            "SELECT model_name, data_type, description FROM columns WHERE column_name = 'amount'"
        ).fetchall()

    stats = _timed(q_models_with_column)
    results["models_with_column"] = stats
    row_count = len(conn.execute(
        "SELECT model_name FROM columns WHERE column_name = 'amount'"
    ).fetchall())
    print(f"  Models with 'amount' col ({row_count} hits):   {_fmt(stats)}")

    # 7) Discovered vs defined columns stats
    def q_discovered_stats():
        conn.execute(
            "SELECT source, COUNT(*) FROM columns GROUP BY source"
        ).fetchall()

    stats = _timed(q_discovered_stats)
    results["discovered_vs_defined"] = stats
    rows = conn.execute("SELECT source, COUNT(*) as cnt FROM columns GROUP BY source").fetchall()
    breakdown = ", ".join(f"{r[0]}={r[1]}" for r in rows)
    print(f"  Discovered vs defined ({breakdown}): {_fmt(stats)}")

    return results


def bench_downstream_queries(conn: sqlite3.Connection) -> dict[str, dict[str, float]]:
    """Benchmark complex downstream lineage traversal queries."""
    print(f"\n{'─' * 70}")
    print("  Complex Downstream Lineage Queries")
    print(f"{'─' * 70}")
    results: dict[str, dict[str, float]] = {}

    # Pick a source that actually has downstream dependents
    popular_source = conn.execute(
        "SELECT source, COUNT(*) as cnt FROM model_dependencies "
        "WHERE source LIKE 'raw_%' GROUP BY source ORDER BY cnt DESC LIMIT 1"
    ).fetchone()
    src_name = popular_source[0] if popular_source else "raw_000"

    # Pick a source that has 'id' in column_lineage
    lineage_source = conn.execute(
        "SELECT source_model FROM column_lineage "
        "WHERE source_model LIKE 'raw_%' AND source_column = 'id' LIMIT 1"
    ).fetchone()
    lineage_src = lineage_source[0] if lineage_source else src_name

    # Pick a report that has column lineage
    report_with_lineage = conn.execute(
        "SELECT target_model FROM column_lineage "
        "WHERE target_model LIKE 'report_%' LIMIT 1"
    ).fetchone()
    report_name = report_with_lineage[0] if report_with_lineage else "report_010"

    # Pick a source.column pair that has impact
    impact_pair = conn.execute(
        "SELECT source_model, source_column FROM column_lineage "
        "WHERE source_model LIKE 'raw_%' AND source_column = 'amount' LIMIT 1"
    ).fetchone()
    if not impact_pair:
        impact_pair = conn.execute(
            "SELECT source_model, source_column FROM column_lineage "
            "WHERE source_model LIKE 'raw_%' LIMIT 1"
        ).fetchone()
    impact_model = impact_pair[0] if impact_pair else src_name
    impact_col = impact_pair[1] if impact_pair else "id"

    # 1) Recursive downstream: all models that depend on a source (via model deps)
    def q_downstream_models():
        conn.execute("""
            WITH RECURSIVE downstream AS (
                SELECT target FROM model_dependencies WHERE source = ?
                UNION
                SELECT md.target FROM model_dependencies md
                JOIN downstream d ON md.source = d.target
            )
            SELECT * FROM downstream
        """, (src_name,)).fetchall()

    stats = _timed(q_downstream_models)
    results["downstream_models_recursive"] = stats
    rows = conn.execute("""
        WITH RECURSIVE downstream AS (
            SELECT target FROM model_dependencies WHERE source = ?
            UNION
            SELECT md.target FROM model_dependencies md
            JOIN downstream d ON md.source = d.target
        )
        SELECT * FROM downstream
    """, (src_name,)).fetchall()
    print(f"  Downstream models from {src_name} ({len(rows)} models): {_fmt(stats)}")

    # 2) Recursive upstream: all models upstream of a report
    def q_upstream_models():
        conn.execute("""
            WITH RECURSIVE upstream AS (
                SELECT source FROM model_dependencies WHERE target = ?
                UNION
                SELECT md.source FROM model_dependencies md
                JOIN upstream u ON md.target = u.source
            )
            SELECT * FROM upstream
        """, (report_name,)).fetchall()

    stats = _timed(q_upstream_models)
    results["upstream_models_recursive"] = stats
    rows = conn.execute("""
        WITH RECURSIVE upstream AS (
            SELECT source FROM model_dependencies WHERE target = ?
            UNION
            SELECT md.source FROM model_dependencies md
            JOIN upstream u ON md.target = u.source
        )
        SELECT * FROM upstream
    """, (report_name,)).fetchall()
    print(f"  Upstream models to {report_name} ({len(rows)} models):  {_fmt(stats)}")

    # 3) Column lineage trace: follow a column from source through all layers
    def q_column_trace():
        conn.execute("""
            WITH RECURSIVE col_trace AS (
                SELECT source_model, source_column, target_model, target_column, transform, 1 as depth
                FROM column_lineage
                WHERE source_model = ? AND source_column = 'id'
                UNION ALL
                SELECT cl.source_model, cl.source_column, cl.target_model, cl.target_column,
                       cl.transform, ct.depth + 1
                FROM column_lineage cl
                JOIN col_trace ct ON cl.source_model = ct.target_model
                    AND cl.source_column = ct.target_column
                WHERE ct.depth < 10
            )
            SELECT * FROM col_trace
        """, (lineage_src,)).fetchall()

    stats = _timed(q_column_trace)
    results["column_trace_downstream"] = stats
    rows = conn.execute("""
        WITH RECURSIVE col_trace AS (
            SELECT source_model, source_column, target_model, target_column, transform, 1 as depth
            FROM column_lineage
            WHERE source_model = ? AND source_column = 'id'
            UNION ALL
            SELECT cl.source_model, cl.source_column, cl.target_model, cl.target_column,
                   cl.transform, ct.depth + 1
            FROM column_lineage cl
            JOIN col_trace ct ON cl.source_model = ct.target_model
                AND cl.source_column = ct.target_column
            WHERE ct.depth < 10
        )
        SELECT * FROM col_trace
    """, (lineage_src,)).fetchall()
    print(f"  Column trace from {lineage_src}.id ({len(rows)} edges):   {_fmt(stats)}")

    # 4) Reverse column lineage: trace back from a report column to its sources
    def q_reverse_column_trace():
        conn.execute("""
            WITH RECURSIVE reverse_trace AS (
                SELECT source_model, source_column, target_model, target_column, transform, 1 as depth
                FROM column_lineage
                WHERE target_model = ?
                UNION ALL
                SELECT cl.source_model, cl.source_column, cl.target_model, cl.target_column,
                       cl.transform, rt.depth + 1
                FROM column_lineage cl
                JOIN reverse_trace rt ON cl.target_model = rt.source_model
                    AND cl.target_column = rt.source_column
                WHERE rt.depth < 10
            )
            SELECT DISTINCT source_model, source_column, target_model, target_column, transform, depth
            FROM reverse_trace ORDER BY depth
        """, (report_name,)).fetchall()

    stats = _timed(q_reverse_column_trace)
    results["reverse_column_trace"] = stats
    rows = conn.execute("""
        WITH RECURSIVE reverse_trace AS (
            SELECT source_model, source_column, target_model, target_column, transform, 1 as depth
            FROM column_lineage
            WHERE target_model = ?
            UNION ALL
            SELECT cl.source_model, cl.source_column, cl.target_model, cl.target_column,
                   cl.transform, rt.depth + 1
            FROM column_lineage cl
            JOIN reverse_trace rt ON cl.target_model = rt.source_model
                AND cl.target_column = rt.source_column
            WHERE rt.depth < 10
        )
        SELECT DISTINCT source_model, source_column FROM reverse_trace
    """, (report_name,)).fetchall()
    print(f"  Reverse trace to {report_name} ({len(rows)} sources):  {_fmt(stats)}")

    # 5) Impact analysis: if we change a column, what models are affected?
    def q_impact_analysis():
        conn.execute("""
            WITH RECURSIVE impact AS (
                SELECT target_model, target_column, transform, 1 as depth
                FROM column_lineage
                WHERE source_model = ? AND source_column = ?
                UNION ALL
                SELECT cl.target_model, cl.target_column, cl.transform, i.depth + 1
                FROM column_lineage cl
                JOIN impact i ON cl.source_model = i.target_model
                    AND cl.source_column = i.target_column
                WHERE i.depth < 10
            )
            SELECT DISTINCT target_model, target_column, MIN(depth) as min_depth
            FROM impact GROUP BY target_model, target_column
            ORDER BY min_depth
        """, (impact_model, impact_col)).fetchall()

    stats = _timed(q_impact_analysis)
    results["impact_analysis"] = stats
    rows = conn.execute("""
        WITH RECURSIVE impact AS (
            SELECT target_model, target_column, transform, 1 as depth
            FROM column_lineage
            WHERE source_model = ? AND source_column = ?
            UNION ALL
            SELECT cl.target_model, cl.target_column, cl.transform, i.depth + 1
            FROM column_lineage cl
            JOIN impact i ON cl.source_model = i.target_model
                AND cl.source_column = i.target_column
            WHERE i.depth < 10
        )
        SELECT DISTINCT target_model FROM impact
    """, (impact_model, impact_col)).fetchall()
    print(f"  Impact of {impact_model}.{impact_col} ({len(rows)} models):      {_fmt(stats)}")

    # 6) Cross-model column join: find all transforms applied to 'amount' across pipeline
    def q_transform_chain():
        conn.execute("""
            SELECT cl.source_model, cl.source_column, cl.target_model, cl.target_column,
                   cl.transform, m.layer
            FROM column_lineage cl
            JOIN models m ON cl.target_model = m.name
            WHERE cl.source_column = 'amount' OR cl.target_column LIKE '%amount%'
            ORDER BY m.layer, cl.target_model
        """).fetchall()

    stats = _timed(q_transform_chain)
    results["transform_chain"] = stats
    row_count = len(conn.execute(
        "SELECT * FROM column_lineage WHERE source_column = 'amount' OR target_column LIKE '%amount%'"
    ).fetchall())
    print(f"  Transform chain for 'amount' ({row_count} rows):   {_fmt(stats)}")

    # 7) Full lineage graph edge count per layer
    def q_lineage_by_layer():
        conn.execute("""
            SELECT m.layer, COUNT(*) as edge_count
            FROM column_lineage cl
            JOIN models m ON cl.target_model = m.name
            GROUP BY m.layer
            ORDER BY m.layer
        """).fetchall()

    stats = _timed(q_lineage_by_layer)
    results["lineage_by_layer"] = stats
    rows = conn.execute("""
        SELECT m.layer, COUNT(*) as edge_count
        FROM column_lineage cl JOIN models m ON cl.target_model = m.name
        GROUP BY m.layer ORDER BY m.layer
    """).fetchall()
    breakdown = ", ".join(f"{r[0]}={r[1]}" for r in rows)
    print(f"  Lineage edges by layer ({breakdown}): {_fmt(stats)}")

    return results


def bench_state_backend_with_lineage(
    models: list[ModelDef], tmp_dir: Path
) -> dict[str, dict[str, float]]:
    """Benchmark the SQLiteBackend with 500 assets that include column metadata."""
    print(f"\n{'─' * 70}")
    print("  SQLiteBackend — 500 Assets with Column Metadata")
    print(f"{'─' * 70}")
    results: dict[str, dict[str, float]] = {}

    backend = SQLiteBackend(db_path=tmp_dir / "state.db")

    # Build snapshot from models
    assets: dict[str, AssetState] = {}
    deps: list[DependencyState] = []
    for m in models:
        fp = hashlib.sha256(f"{m.name}:{m.sql or ''}".encode()).hexdigest()
        assets[m.name] = AssetState(
            name=m.name, kind=m.layer, fingerprint=fp,
            data={
                "name": m.name,
                "description": m.description,
                "sql": m.sql,
                "columns": m.all_columns,
                "column_lineage": [
                    {"src_model": s, "src_col": sc, "tgt_col": tc, "transform": t}
                    for s, sc, tc, t in m.column_lineage
                ],
            },
            applied_by="benchmark", version=1,
        )
        for dep_name in m.depends_on:
            dep_fp = hashlib.sha256(f"{dep_name}:{m.name}".encode()).hexdigest()
            deps.append(DependencyState(
                source=dep_name, target=m.name, type="ref", fingerprint=dep_fp,
            ))

    snapshot = StateSnapshot(
        environment=ENVIRONMENT, assets=assets, dependencies=deps,
        metadata={"benchmark": "column_lineage", "num_assets": len(models)},
    )

    # Save
    stats = _timed(lambda: backend.save(ENVIRONMENT, snapshot))
    results["save"] = stats
    print(f"  Save {len(models)} assets with columns:     {_fmt(stats)}")

    # Load
    stats = _timed(lambda: backend.load(ENVIRONMENT))
    results["load"] = stats
    print(f"  Load {len(models)} assets with columns:     {_fmt(stats)}")

    # Verify
    loaded = backend.load(ENVIRONMENT)
    assert loaded is not None
    assert len(loaded.assets) == len(models)

    backend.close()
    return results


# ── Main ─────────────────────────────────────────────────────

def main() -> None:
    print("=" * 70)
    print(f"  BENCHMARK: Column Lineage with sqlglot — {NUM_ASSETS} models")
    print("=" * 70)

    tmp_root = Path(tempfile.mkdtemp(prefix="assets_lineage_bench_"))
    print(f"  Temp dir: {tmp_root}")

    all_results: dict[str, Any] = {}

    # ── Step 1: Generate DAG ──
    print(f"\n  Generating {NUM_ASSETS}-model DAG...")
    t0 = time.perf_counter()
    models, schema = generate_dag()
    gen_ms = (time.perf_counter() - t0) * 1000
    print(f"  DAG generated in {gen_ms:.1f}ms")
    print(f"  Layers: sources={NUM_SOURCES} staging={NUM_STAGING} "
          f"intermediate={NUM_INTERMEDIATE} marts={NUM_MARTS} reports={NUM_REPORTS}")

    # ── Step 2: Extract lineage with sqlglot ──
    print(f"\n  Extracting column lineage with sqlglot...")
    t0 = time.perf_counter()
    for m in models:
        extract_lineage_for_model(m, schema)
        # Update schema with discovered columns for downstream models
        if m.all_columns:
            schema[m.name] = {c: v.get("type", "VARCHAR") for c, v in m.all_columns.items()}
    lineage_ms = (time.perf_counter() - t0) * 1000
    print(f"  Lineage extracted in {lineage_ms:.1f}ms")

    total_cols = sum(len(m.all_columns) for m in models)
    defined_cols = sum(len(m.defined_columns) for m in models)
    discovered_cols = total_cols - defined_cols
    total_edges = sum(len(m.column_lineage) for m in models)
    print(f"  Total columns: {total_cols} (defined={defined_cols}, discovered={discovered_cols})")
    print(f"  Total lineage edges: {total_edges}")
    all_results["generation"] = {"dag_ms": gen_ms, "lineage_ms": lineage_ms,
                                  "total_cols": total_cols, "lineage_edges": total_edges}

    # ── Step 3: Store in SQLite ──
    db_path = tmp_root / "lineage.db"
    print(f"\n  Storing in SQLite...")
    t0 = time.perf_counter()
    conn = create_lineage_db(db_path)
    store_lineage(conn, models)
    store_ms = (time.perf_counter() - t0) * 1000
    print(f"  Stored in {store_ms:.1f}ms")
    all_results["storage_ms"] = store_ms

    # Verify counts
    model_count = conn.execute("SELECT COUNT(*) FROM models").fetchone()[0]
    col_count = conn.execute("SELECT COUNT(*) FROM columns").fetchone()[0]
    lineage_count = conn.execute("SELECT COUNT(*) FROM column_lineage").fetchone()[0]
    dep_count = conn.execute("SELECT COUNT(*) FROM model_dependencies").fetchone()[0]
    print(f"  DB: {model_count} models, {col_count} columns, "
          f"{lineage_count} lineage edges, {dep_count} dependency edges")

    # ── Step 4: Simple queries ──
    all_results["simple_queries"] = bench_simple_queries(conn)

    # ── Step 5: Complex downstream queries ──
    all_results["downstream_queries"] = bench_downstream_queries(conn)

    # ── Step 6: State backend benchmark with column data ──
    all_results["state_backend"] = bench_state_backend_with_lineage(models, tmp_root)

    conn.close()

    # ── Summary ──
    print(f"\n{'=' * 70}")
    print("  SUMMARY")
    print(f"{'=' * 70}")
    print(f"\n  DAG generation:         {all_results['generation']['dag_ms']:>10.1f} ms")
    print(f"  Lineage extraction:     {all_results['generation']['lineage_ms']:>10.1f} ms")
    print(f"  SQLite storage:         {all_results['storage_ms']:>10.1f} ms")
    print(f"  Total columns:          {all_results['generation']['total_cols']:>10d}")
    print(f"  Lineage edges:          {all_results['generation']['lineage_edges']:>10d}")

    print(f"\n  {'Simple Query':<35} {'Median ms':>10}")
    print(f"  {'─' * 45}")
    for k, v in all_results["simple_queries"].items():
        print(f"  {k:<35} {v['median_ms']:>10.3f}")

    print(f"\n  {'Complex Query':<35} {'Median ms':>10}")
    print(f"  {'─' * 45}")
    for k, v in all_results["downstream_queries"].items():
        print(f"  {k:<35} {v['median_ms']:>10.3f}")

    print(f"\n  {'State Backend':<35} {'Median ms':>10}")
    print(f"  {'─' * 45}")
    for k, v in all_results["state_backend"].items():
        print(f"  {k:<35} {v['median_ms']:>10.3f}")

    print()

    # Cleanup
    shutil.rmtree(tmp_root, ignore_errors=True)
    print(f"  Cleaned up {tmp_root}")


if __name__ == "__main__":
    main()
