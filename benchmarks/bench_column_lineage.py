"""Benchmark — Column lineage with sqlglot over realistic analytical models in SQLite.

Run:
    python benchmarks/bench_column_lineage.py          # 50 models (quick test)
    python benchmarks/bench_column_lineage.py --scale 10  # 500 models (full)

Generates a multi-layer DAG with {{ ref('model') }} Jinja syntax, CTE-heavy
analytical SQL (like dbt/sqlmesh), resolves refs, then uses sqlglot.lineage()
for proper through-CTE column tracing. Merges discovered columns with manually-
defined descriptions and stores everything in SQLite.
"""

from __future__ import annotations

import hashlib
import json
import random
import re
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
from sqlglot.lineage import lineage as sqlglot_lineage

from assets import SQLiteBackend
from assets.state.models import AssetState, DependencyState, StateSnapshot

# ── Configuration (scale=1 → 50 models, scale=10 → 500) ─────
DEFAULT_SCALE = 1
ITERATIONS = 5
ENVIRONMENT = "production"
SEED = 42

# Column type mapping
COL_TYPES: dict[str, str] = {
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
    "region": "VARCHAR", "segment": "VARCHAR", "channel": "VARCHAR",
    "platform": "VARCHAR", "device_type": "VARCHAR",
}

COL_DESCRIPTIONS: dict[str, str] = {
    "id": "Primary key identifier",
    "user_id": "Foreign key to the users table",
    "email": "User email address",
    "amount": "Transaction amount in base currency",
    "total_revenue": "Aggregated total revenue",
    "avg_order_value": "Average order value",
    "order_count": "Number of orders",
    "status": "Current status of the record",
    "created_at": "Timestamp when the record was created",
    "category": "Product category classification",
    "country": "Geographic country",
    "region": "Geographic region",
}

# Column pools for source tables
_SOURCE_COL_POOLS = [
    ["id", "uuid", "external_id"],
    ["created_at", "updated_at", "deleted_at"],
    ["user_id", "username", "email", "first_name", "last_name", "country"],
    ["order_id", "amount", "currency", "status", "discount", "tax"],
    ["product_id", "product_name", "category", "price", "sku", "brand"],
    ["region", "segment", "channel", "platform"],
]

REF_PATTERN = re.compile(r"\{\{\s*ref\(['\"](\w+)['\"]\)\s*\}\}")


# ── Model definition ─────────────────────────────────────────

class ModelDef:
    """Holds a generated model with Jinja SQL, resolved SQL, and lineage."""

    __slots__ = ("name", "layer", "description", "jinja_sql", "resolved_sql",
                 "depends_on", "defined_columns", "all_columns", "column_lineage")

    def __init__(
        self,
        name: str,
        layer: str,
        description: str,
        jinja_sql: str | None,
        depends_on: list[str],
        defined_columns: dict[str, dict[str, str]],
    ):
        self.name = name
        self.layer = layer
        self.description = description
        self.jinja_sql = jinja_sql  # SQL with {{ ref('...') }}
        self.resolved_sql: str | None = None  # after ref resolution
        self.depends_on = depends_on
        self.defined_columns = defined_columns
        self.all_columns: dict[str, dict[str, str]] = {}
        self.column_lineage: list[tuple[str, str, str, str | None]] = []


# ── Ref resolution ────────────────────────────────────────────

def resolve_refs(jinja_sql: str) -> tuple[str, list[str]]:
    """Replace {{ ref('model') }} with the model name. Return (sql, refs)."""
    refs: list[str] = []

    def _replace(match: re.Match) -> str:
        ref_name = match.group(1)
        refs.append(ref_name)
        return ref_name

    resolved = REF_PATTERN.sub(_replace, jinja_sql)
    return resolved, refs


# ── Column helpers ────────────────────────────────────────────

def _pick_columns(rng: random.Random, n: int) -> list[str]:
    pool: list[str] = []
    for p in _SOURCE_COL_POOLS:
        pool.extend(p)
    chosen = rng.sample(pool, min(n, len(pool)))
    if "id" not in chosen:
        chosen[0] = "id"
    return chosen


def _make_col_defs(
    cols: list[str],
    rng: random.Random,
    describe_frac: float = 0.7,
    include_frac: float = 1.0,
) -> dict[str, dict[str, str]]:
    """Create column definitions; skip some to test sqlglot discovery."""
    defs: dict[str, dict[str, str]] = {}
    for c in cols:
        if rng.random() > include_frac:
            continue
        entry: dict[str, str] = {"type": COL_TYPES.get(c, "VARCHAR")}
        if rng.random() < describe_frac and c in COL_DESCRIPTIONS:
            entry["description"] = COL_DESCRIPTIONS[c]
        defs[c] = entry
    return defs


# ── DAG generator with realistic CTE SQL ─────────────────────

def generate_dag(
    scale: int = DEFAULT_SCALE,
    seed: int = SEED,
) -> tuple[list[ModelDef], dict[str, dict[str, str]]]:
    """Build a multi-layer DAG with CTE-heavy analytical SQL.

    scale=1 → 50 models, scale=10 → 500 models.
    """
    n_sources = 5 * scale
    n_staging = 10 * scale
    n_intermediate = 15 * scale
    n_marts = 15 * scale
    n_reports = 5 * scale

    rng = random.Random(seed)
    models: list[ModelDef] = []
    schema: dict[str, dict[str, str]] = {}
    models_by_name: dict[str, ModelDef] = {}

    def _register(m: ModelDef, output_cols: dict[str, dict[str, str]]) -> None:
        models.append(m)
        models_by_name[m.name] = m
        schema[m.name] = {c: v.get("type", "VARCHAR") for c, v in output_cols.items()}

    # ── Layer 0: Sources (raw tables, no SQL) ──
    for i in range(n_sources):
        name = f"raw_{i:03d}"
        cols = _pick_columns(rng, rng.randint(5, 10))
        col_defs = _make_col_defs(cols, rng, describe_frac=0.9)
        m = ModelDef(name=name, layer="source", description=f"Raw source table {i}",
                     jinja_sql=None, depends_on=[], defined_columns=col_defs)
        m.all_columns = dict(col_defs)
        _register(m, col_defs)

    source_names = [m.name for m in models if m.layer == "source"]

    # ── Layer 1: Staging (CTE with filtering + cleaning) ──
    for i in range(n_staging):
        name = f"stg_{i:03d}"
        src = rng.choice(source_names)
        src_cols = list(schema[src].keys())
        selected = rng.sample(src_cols, min(rng.randint(3, len(src_cols)), len(src_cols)))
        if "id" not in selected:
            selected[0] = "id"

        # Build CTE SQL with {{ ref() }}
        cte_cols = ", ".join(f"src.{c}" for c in selected)
        # Some columns get cleaned
        final_parts = []
        for c in selected:
            if rng.random() < 0.15 and c not in ("id",):
                final_parts.append(f"COALESCE(cleaned.{c}, '') AS {c}_cleaned")
            else:
                final_parts.append(f"cleaned.{c}")

        jinja = (
            f"WITH source AS (\n"
            f"    SELECT {cte_cols}\n"
            f"    FROM {{{{ ref('{src}') }}}} src\n"
            f"),\n"
            f"cleaned AS (\n"
            f"    SELECT *\n"
            f"    FROM source\n"
            f"    WHERE id IS NOT NULL\n"
            f")\n"
            f"SELECT {', '.join(final_parts)}\n"
            f"FROM cleaned"
        )
        output_cols_list = []
        for p in final_parts:
            if " AS " in p:
                output_cols_list.append(p.split(" AS ")[-1].strip())
            else:
                output_cols_list.append(p.split(".")[-1].strip())

        col_defs = _make_col_defs(output_cols_list, rng, describe_frac=0.6, include_frac=0.6)
        m = ModelDef(name=name, layer="staging", description=f"Staging from {src}",
                     jinja_sql=jinja, depends_on=[src], defined_columns=col_defs)
        _register(m, _make_col_defs(output_cols_list, rng, describe_frac=1.0))

    staging_names = [m.name for m in models if m.layer == "staging"]

    # ── Layer 2: Intermediate (CTE with JOINs across staging) ──
    for i in range(n_intermediate):
        name = f"int_{i:03d}"
        n_deps = rng.randint(2, min(3, len(staging_names)))
        deps = rng.sample(staging_names, n_deps)
        base, *joins = deps

        base_cols = list(schema.get(base, {}).keys())
        if not base_cols:
            base_cols = ["id"]

        # CTE: first select from base, then join with others
        base_select = ", ".join(f"base_tbl.{c}" for c in base_cols[:5])
        join_selects = []
        join_clauses = []
        for j, dep in enumerate(joins):
            alias = f"j{j}"
            dep_cols = [c for c in list(schema.get(dep, {}).keys()) if c != "id"][:3]
            for c in dep_cols:
                join_selects.append(f"{alias}.{c}")
            join_clauses.append(
                f"    JOIN {{{{ ref('{dep}') }}}} {alias} ON base_tbl.id = {alias}.id"
            )

        all_selects = base_select
        if join_selects:
            all_selects += ", " + ", ".join(join_selects)

        jinja = (
            f"WITH joined AS (\n"
            f"    SELECT {all_selects}\n"
            f"    FROM {{{{ ref('{base}') }}}} base_tbl\n"
            f"{chr(10).join(join_clauses)}\n"
            f"),\n"
            f"deduped AS (\n"
            f"    SELECT DISTINCT *\n"
            f"    FROM joined\n"
            f")\n"
            f"SELECT * FROM deduped"
        )

        # Output columns = base cols + join cols
        out_cols = list(base_cols[:5])
        for dep in joins:
            dep_cols = [c for c in list(schema.get(dep, {}).keys()) if c != "id"][:3]
            out_cols.extend(dep_cols)
        # Deduplicate
        seen: set[str] = set()
        unique_out: list[str] = []
        for c in out_cols:
            if c not in seen:
                seen.add(c)
                unique_out.append(c)

        col_defs = _make_col_defs(unique_out, rng, describe_frac=0.5, include_frac=0.5)
        m = ModelDef(name=name, layer="intermediate", description=f"Join of {', '.join(deps)}",
                     jinja_sql=jinja, depends_on=deps, defined_columns=col_defs)
        _register(m, _make_col_defs(unique_out, rng, describe_frac=1.0))

    int_names = [m.name for m in models if m.layer == "intermediate"]

    # ── Layer 3: Marts (CTE with filtering → aggregation) ──
    for i in range(n_marts):
        name = f"mart_{i:03d}"
        base = rng.choice(int_names)
        deps = [base]
        base_cols = list(schema.get(base, {}).keys())
        if not base_cols:
            base_cols = ["id"]

        # Pick group-by and agg columns
        group_candidates = [c for c in base_cols if c in (
            "country", "region", "segment", "category", "channel", "status", "platform")]
        group_cols = group_candidates[:2] if group_candidates else ["id"]
        agg_candidates = [c for c in base_cols if c in (
            "amount", "total", "price", "tax", "discount")]
        agg_cols = agg_candidates[:2] if agg_candidates else []

        # Sometimes join with another staging model
        extra_cte = ""
        extra_join = ""
        if rng.random() < 0.4 and staging_names:
            extra = rng.choice(staging_names)
            deps.append(extra)
            extra_cte = (
                f"enrichment AS (\n"
                f"    SELECT id, * FROM {{{{ ref('{extra}') }}}}\n"
                f"),\n"
            )
            extra_join = f"\n    LEFT JOIN enrichment e ON filtered.id = e.id"

        # Build CTE
        group_select = ", ".join(f"ready.{c}" for c in group_cols)
        agg_select_parts = []
        for c in agg_cols:
            agg_select_parts.append(f"SUM(ready.{c}) AS total_{c}")
            agg_select_parts.append(f"AVG(ready.{c}) AS avg_{c}")
        agg_select_parts.append("COUNT(*) AS record_count")
        agg_select = ", ".join(agg_select_parts)
        group_by = ", ".join(f"ready.{c}" for c in group_cols)

        jinja = (
            f"WITH filtered AS (\n"
            f"    SELECT *\n"
            f"    FROM {{{{ ref('{base}') }}}}\n"
            f"    WHERE id IS NOT NULL\n"
            f"),\n"
            f"{extra_cte}"
            f"ready AS (\n"
            f"    SELECT filtered.*\n"
            f"    FROM filtered{extra_join}\n"
            f")\n"
            f"SELECT {group_select}, {agg_select}\n"
            f"FROM ready\n"
            f"GROUP BY {group_by}"
        )

        out_cols = list(group_cols)
        for c in agg_cols:
            out_cols.extend([f"total_{c}", f"avg_{c}"])
        out_cols.append("record_count")

        col_defs = _make_col_defs(out_cols, rng, describe_frac=0.4, include_frac=0.5)
        m = ModelDef(name=name, layer="mart", description=f"Mart from {base}",
                     jinja_sql=jinja, depends_on=deps, defined_columns=col_defs)
        _register(m, _make_col_defs(out_cols, rng, describe_frac=1.0))

    mart_names = [m.name for m in models if m.layer == "mart"]

    # ── Layer 4: Reports (CTE with final transforms from marts) ──
    for i in range(n_reports):
        name = f"report_{i:03d}"
        src = rng.choice(mart_names)
        deps = [src]
        src_cols = list(schema.get(src, {}).keys())
        if not src_cols:
            src_cols = ["id"]

        col_list = ", ".join(f"base.{c}" for c in src_cols[:5])
        jinja = (
            f"WITH base AS (\n"
            f"    SELECT *\n"
            f"    FROM {{{{ ref('{src}') }}}}\n"
            f"),\n"
            f"final AS (\n"
            f"    SELECT {col_list}\n"
            f"    FROM base\n"
            f"    ORDER BY 1\n"
            f")\n"
            f"SELECT * FROM final"
        )

        out_cols = src_cols[:5]
        col_defs = _make_col_defs(out_cols, rng, describe_frac=0.3, include_frac=0.4)
        m = ModelDef(name=name, layer="report", description=f"Report from {src}",
                     jinja_sql=jinja, depends_on=deps, defined_columns=col_defs)
        _register(m, _make_col_defs(out_cols, rng, describe_frac=1.0))

    return models, schema


# ── Lineage extraction with sqlglot.lineage() ────────────────

def extract_lineage_for_model(
    model: ModelDef,
    schema: dict[str, dict[str, str]],
) -> None:
    """Resolve refs, then use sqlglot.lineage() for through-CTE column tracing.

    - Resolves {{ ref('model') }} → plain table name
    - Parses output columns from the AST
    - Calls sqlglot.lineage() per output column for full CTE resolution
    - Columns found by sqlglot but missing from defined_columns are added
      without descriptions (marked as "discovered")
    """
    if model.jinja_sql is None:
        model.all_columns = dict(model.defined_columns)
        return

    # Step 1: Resolve refs
    resolved, refs = resolve_refs(model.jinja_sql)
    model.resolved_sql = resolved

    # Start with defined columns
    model.all_columns = dict(model.defined_columns)
    model.column_lineage = []

    # Step 2: Parse to discover output columns
    try:
        parsed = sqlglot.parse_one(resolved)
    except Exception:
        return

    output_cols: list[str] = []
    for sel in parsed.selects:
        col_name = sel.alias_or_name
        if col_name and col_name != "*":
            output_cols.append(col_name)
            if col_name not in model.all_columns:
                model.all_columns[col_name] = {"type": COL_TYPES.get(col_name, "VARCHAR")}

    # For SELECT *, expand from schema of first dependency
    if not output_cols and model.depends_on:
        first_dep = model.depends_on[0]
        if first_dep in schema:
            output_cols = list(schema[first_dep].keys())
            for c in output_cols:
                if c not in model.all_columns:
                    model.all_columns[c] = {"type": COL_TYPES.get(c, "VARCHAR")}

    # Step 3: Use sqlglot.lineage() for each output column — traces through CTEs
    for col_name in output_cols:
        try:
            node = sqlglot_lineage(col_name, resolved, schema=schema)
            _collect_leaf_sources(node, model.name, col_name, model)
        except Exception:
            pass


def _collect_leaf_sources(
    node: Any,
    target_model: str,
    target_col: str,
    model: ModelDef,
) -> None:
    """Recursively walk lineage tree to find leaf (actual table) sources."""
    if not node.downstream:
        # Leaf node — this is a real table reference
        src_table = ""
        if hasattr(node.source, "this") and hasattr(node.source.this, "this"):
            src_table = node.source.this.this
        elif hasattr(node.source, "name"):
            src_table = node.source.name

        col_name = node.name
        if "." in col_name:
            col_name = col_name.split(".")[-1]

        # Detect transform from the parent expression
        transform: str | None = None
        expr_sql = str(node.expression) if node.expression else ""
        for fn in ("SUM", "AVG", "COUNT", "COALESCE", "MIN", "MAX"):
            if fn in expr_sql.upper():
                transform = fn
                break

        if src_table and src_table not in ("", "*"):
            model.column_lineage.append((src_table, col_name, target_col, transform))
    else:
        # Detect transform at intermediate nodes
        transform: str | None = None
        source_str = str(node.source) if node.source else ""
        for fn in ("SUM", "AVG", "COUNT"):
            if fn + "(" in source_str.upper():
                transform = fn
                break

        for child in node.downstream:
            # Pass transform info down if this is an aggregate node
            _collect_leaf_sources(child, target_model, target_col, model)

        # If we found a transform at this level, update the lineage entries
        if transform:
            for j in range(len(model.column_lineage)):
                src_m, src_c, tgt_c, existing_t = model.column_lineage[j]
                if tgt_c == target_col and existing_t is None:
                    model.column_lineage[j] = (src_m, src_c, tgt_c, transform)


# ── SQLite lineage schema ────────────────────────────────────

LINEAGE_SCHEMA = """\
CREATE TABLE IF NOT EXISTS models (
    name        TEXT PRIMARY KEY,
    layer       TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    jinja_sql   TEXT,
    resolved_sql TEXT,
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
CREATE INDEX IF NOT EXISTS idx_col_name         ON columns(column_name);
CREATE INDEX IF NOT EXISTS idx_lineage_target    ON column_lineage(target_model, target_column);
CREATE INDEX IF NOT EXISTS idx_lineage_source    ON column_lineage(source_model, source_column);
CREATE INDEX IF NOT EXISTS idx_deps_source       ON model_dependencies(source);
CREATE INDEX IF NOT EXISTS idx_deps_target       ON model_dependencies(target);
"""


def create_lineage_db(db_path: Path) -> sqlite3.Connection:
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
    with conn:
        conn.executemany(
            "INSERT OR REPLACE INTO models (name, layer, description, jinja_sql, resolved_sql, fingerprint) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ((m.name, m.layer, m.description, m.jinja_sql, m.resolved_sql,
              hashlib.sha256(f"{m.name}:{m.resolved_sql or ''}".encode()).hexdigest())
             for m in models),
        )
        conn.executemany(
            "INSERT OR REPLACE INTO model_dependencies (source, target) VALUES (?, ?)",
            ((dep, m.name) for m in models for dep in m.depends_on),
        )
        conn.executemany(
            "INSERT OR REPLACE INTO columns (model_name, column_name, data_type, description, source) "
            "VALUES (?, ?, ?, ?, ?)",
            ((m.name, col, info.get("type", "VARCHAR"), info.get("description", ""),
              "defined" if col in m.defined_columns else "discovered")
             for m in models for col, info in m.all_columns.items()),
        )
        conn.executemany(
            "INSERT INTO column_lineage (source_model, source_column, target_model, target_column, transform) "
            "VALUES (?, ?, ?, ?, ?)",
            ((sm, sc, m.name, tc, tr)
             for m in models for sm, sc, tc, tr in m.column_lineage),
        )


# ── Benchmark helpers ────────────────────────────────────────

def _timed(fn, iterations: int = ITERATIONS) -> dict[str, float]:
    times = []
    for _ in range(iterations):
        t0 = time.perf_counter()
        fn()
        times.append((time.perf_counter() - t0) * 1000)
    return {
        "min_ms": min(times), "max_ms": max(times),
        "mean_ms": statistics.mean(times), "median_ms": statistics.median(times),
        "stdev_ms": statistics.stdev(times) if len(times) > 1 else 0.0,
    }


def _fmt(stats: dict[str, float]) -> str:
    return (f"  mean={stats['mean_ms']:8.2f}ms  median={stats['median_ms']:8.2f}ms  "
            f"min={stats['min_ms']:8.2f}ms  max={stats['max_ms']:8.2f}ms  "
            f"stdev={stats['stdev_ms']:7.2f}ms")


# ── Query benchmarks ─────────────────────────────────────────

def bench_simple_queries(conn: sqlite3.Connection) -> dict[str, dict[str, float]]:
    print(f"\n{'─' * 70}")
    print("  Simple Queries")
    print(f"{'─' * 70}")
    results: dict[str, dict[str, float]] = {}

    # Pick a model that has columns
    sample = conn.execute(
        "SELECT model_name FROM columns GROUP BY model_name ORDER BY COUNT(*) DESC LIMIT 1"
    ).fetchone()
    sample_model = sample[0] if sample else "stg_000"

    # 1) Columns for single model
    stats = _timed(lambda: conn.execute(
        "SELECT column_name, data_type, description FROM columns WHERE model_name = ?",
        (sample_model,)).fetchall())
    results["columns_single_model"] = stats
    n = len(conn.execute("SELECT * FROM columns WHERE model_name = ?", (sample_model,)).fetchall())
    print(f"  Columns for {sample_model} ({n} cols):      {_fmt(stats)}")

    # 2) All columns
    stats = _timed(lambda: conn.execute(
        "SELECT model_name, column_name, data_type FROM columns").fetchall())
    results["all_columns"] = stats
    total = conn.execute("SELECT COUNT(*) FROM columns").fetchone()[0]
    print(f"  All columns ({total} total):              {_fmt(stats)}")

    # 3) Column count per model
    stats = _timed(lambda: conn.execute(
        "SELECT model_name, COUNT(*) FROM columns GROUP BY model_name ORDER BY 2 DESC").fetchall())
    results["col_count_per_model"] = stats
    print(f"  Column count per model (GROUP BY):      {_fmt(stats)}")

    # 4) Models with 'amount' column
    stats = _timed(lambda: conn.execute(
        "SELECT model_name, description FROM columns WHERE column_name = 'amount'").fetchall())
    results["models_with_column"] = stats
    n = len(conn.execute("SELECT * FROM columns WHERE column_name = 'amount'").fetchall())
    print(f"  Models with 'amount' ({n} hits):          {_fmt(stats)}")

    # 5) Discovered vs defined
    stats = _timed(lambda: conn.execute(
        "SELECT source, COUNT(*) FROM columns GROUP BY source").fetchall())
    results["discovered_vs_defined"] = stats
    rows = conn.execute("SELECT source, COUNT(*) FROM columns GROUP BY source").fetchall()
    breakdown = ", ".join(f"{r[0]}={r[1]}" for r in rows)
    print(f"  Discovered vs defined ({breakdown}): {_fmt(stats)}")

    # 6) Upstream lineage for one column
    lineage_row = conn.execute(
        "SELECT target_model, target_column FROM column_lineage LIMIT 1").fetchone()
    if lineage_row:
        tm, tc = lineage_row[0], lineage_row[1]
        stats = _timed(lambda: conn.execute(
            "SELECT source_model, source_column, transform FROM column_lineage "
            "WHERE target_model = ? AND target_column = ?", (tm, tc)).fetchall())
        results["upstream_one_column"] = stats
        n = len(conn.execute(
            "SELECT * FROM column_lineage WHERE target_model = ? AND target_column = ?",
            (tm, tc)).fetchall())
        print(f"  Upstream lineage for {tm}.{tc} ({n}):  {_fmt(stats)}")

    return results


def bench_downstream_queries(conn: sqlite3.Connection) -> dict[str, dict[str, float]]:
    print(f"\n{'─' * 70}")
    print("  Complex Downstream Lineage Queries")
    print(f"{'─' * 70}")
    results: dict[str, dict[str, float]] = {}

    # Find good query targets
    pop = conn.execute(
        "SELECT source, COUNT(*) c FROM model_dependencies WHERE source LIKE 'raw_%' "
        "GROUP BY source ORDER BY c DESC LIMIT 1").fetchone()
    src_name = pop[0] if pop else "raw_000"

    report = conn.execute(
        "SELECT target_model FROM column_lineage WHERE target_model LIKE 'report_%' LIMIT 1"
    ).fetchone()
    report_name = report[0] if report else "report_000"

    # Pick a (source_model, source_column) pair that has downstream continuations
    lin_src = conn.execute(
        "SELECT cl.source_model, cl.source_column, COUNT(*) c "
        "FROM column_lineage cl WHERE cl.source_model LIKE 'raw_%' "
        "GROUP BY cl.source_model, cl.source_column ORDER BY c DESC LIMIT 1"
    ).fetchone()
    lineage_src = lin_src[0] if lin_src else src_name
    lineage_col = lin_src[1] if lin_src else "id"

    # Pick a source pair with multi-hop downstream reach
    impact = conn.execute(
        "SELECT cl1.source_model, cl1.source_column FROM column_lineage cl1 "
        "JOIN column_lineage cl2 ON cl1.target_model = cl2.source_model "
        "WHERE cl1.source_model LIKE 'raw_%' "
        "GROUP BY cl1.source_model, cl1.source_column "
        "ORDER BY COUNT(*) DESC LIMIT 1"
    ).fetchone()
    impact_model = impact[0] if impact else lineage_src
    impact_col = impact[1] if impact else lineage_col

    # 1) Downstream models (recursive)
    q = """WITH RECURSIVE ds AS (
        SELECT target FROM model_dependencies WHERE source = ?
        UNION SELECT md.target FROM model_dependencies md JOIN ds ON md.source = ds.target
    ) SELECT * FROM ds"""
    stats = _timed(lambda: conn.execute(q, (src_name,)).fetchall())
    results["downstream_models"] = stats
    rows = conn.execute(q, (src_name,)).fetchall()
    print(f"  Downstream from {src_name} ({len(rows)} models):   {_fmt(stats)}")

    # 2) Upstream models (recursive)
    q = """WITH RECURSIVE us AS (
        SELECT source FROM model_dependencies WHERE target = ?
        UNION SELECT md.source FROM model_dependencies md JOIN us ON md.target = us.source
    ) SELECT * FROM us"""
    stats = _timed(lambda: conn.execute(q, (report_name,)).fetchall())
    results["upstream_models"] = stats
    rows = conn.execute(q, (report_name,)).fetchall()
    print(f"  Upstream to {report_name} ({len(rows)} models):     {_fmt(stats)}")

    # 3) Column trace downstream
    q = """WITH RECURSIVE ct AS (
        SELECT source_model, source_column, target_model, target_column, transform, 1 d
        FROM column_lineage WHERE source_model = ? AND source_column = ?
        UNION ALL
        SELECT cl.source_model, cl.source_column, cl.target_model, cl.target_column,
               cl.transform, ct.d + 1
        FROM column_lineage cl JOIN ct ON cl.source_model = ct.target_model
            AND cl.source_column = ct.target_column WHERE ct.d < 10
    ) SELECT * FROM ct"""
    stats = _timed(lambda: conn.execute(q, (lineage_src, lineage_col)).fetchall())
    results["column_trace_downstream"] = stats
    rows = conn.execute(q, (lineage_src, lineage_col)).fetchall()
    print(f"  Column trace {lineage_src}.{lineage_col} ({len(rows)} edges):  {_fmt(stats)}")

    # 4) Reverse column trace
    q = """WITH RECURSIVE rt AS (
        SELECT source_model, source_column, target_model, target_column, transform, 1 d
        FROM column_lineage WHERE target_model = ?
        UNION ALL
        SELECT cl.source_model, cl.source_column, cl.target_model, cl.target_column,
               cl.transform, rt.d + 1
        FROM column_lineage cl JOIN rt ON cl.target_model = rt.source_model
            AND cl.target_column = rt.source_column WHERE rt.d < 10
    ) SELECT DISTINCT source_model, source_column, target_model, target_column, d
      FROM rt ORDER BY d"""
    stats = _timed(lambda: conn.execute(q, (report_name,)).fetchall())
    results["reverse_trace"] = stats
    rows = conn.execute(q, (report_name,)).fetchall()
    print(f"  Reverse trace to {report_name} ({len(rows)} edges): {_fmt(stats)}")

    # 5) Impact analysis
    q = """WITH RECURSIVE imp AS (
        SELECT target_model, target_column, transform, 1 d
        FROM column_lineage WHERE source_model = ? AND source_column = ?
        UNION ALL
        SELECT cl.target_model, cl.target_column, cl.transform, imp.d + 1
        FROM column_lineage cl JOIN imp ON cl.source_model = imp.target_model
            AND cl.source_column = imp.target_column WHERE imp.d < 10
    ) SELECT DISTINCT target_model, target_column, MIN(d) FROM imp
      GROUP BY target_model, target_column ORDER BY 3"""
    stats = _timed(lambda: conn.execute(q, (impact_model, impact_col)).fetchall())
    results["impact_analysis"] = stats
    rows = conn.execute(q, (impact_model, impact_col)).fetchall()
    print(f"  Impact of {impact_model}.{impact_col} ({len(rows)} cols):  {_fmt(stats)}")

    # 6) Transform chain for 'amount'
    q = """SELECT cl.source_model, cl.source_column, cl.target_model, cl.target_column,
                  cl.transform, m.layer
           FROM column_lineage cl JOIN models m ON cl.target_model = m.name
           WHERE cl.source_column LIKE '%amount%' OR cl.target_column LIKE '%amount%'
           ORDER BY m.layer"""
    stats = _timed(lambda: conn.execute(q).fetchall())
    results["transform_chain"] = stats
    n = len(conn.execute(q).fetchall())
    print(f"  Transform chain for 'amount' ({n} rows):   {_fmt(stats)}")

    # 7) Lineage edges by layer
    q = """SELECT m.layer, COUNT(*) FROM column_lineage cl
           JOIN models m ON cl.target_model = m.name GROUP BY m.layer ORDER BY m.layer"""
    stats = _timed(lambda: conn.execute(q).fetchall())
    results["lineage_by_layer"] = stats
    rows = conn.execute(q).fetchall()
    breakdown = ", ".join(f"{r[0]}={r[1]}" for r in rows)
    print(f"  Lineage by layer ({breakdown}): {_fmt(stats)}")

    return results


def bench_state_backend(
    models: list[ModelDef], tmp_dir: Path
) -> dict[str, dict[str, float]]:
    n = len(models)
    print(f"\n{'─' * 70}")
    print(f"  SQLiteBackend — {n} Assets with Column Metadata")
    print(f"{'─' * 70}")
    results: dict[str, dict[str, float]] = {}
    backend = SQLiteBackend(db_path=tmp_dir / "state.db")

    assets: dict[str, AssetState] = {}
    deps: list[DependencyState] = []
    for m in models:
        fp = hashlib.sha256(f"{m.name}:{m.resolved_sql or ''}".encode()).hexdigest()
        assets[m.name] = AssetState(
            name=m.name, kind=m.layer, fingerprint=fp,
            data={"description": m.description, "sql": m.resolved_sql,
                  "columns": m.all_columns,
                  "lineage": [{"s": s, "sc": sc, "tc": tc, "t": t}
                              for s, sc, tc, t in m.column_lineage]},
            applied_by="benchmark", version=1)
        for d in m.depends_on:
            deps.append(DependencyState(
                source=d, target=m.name, type="ref",
                fingerprint=hashlib.sha256(f"{d}:{m.name}".encode()).hexdigest()))

    snapshot = StateSnapshot(environment=ENVIRONMENT, assets=assets, dependencies=deps,
                             metadata={"benchmark": "column_lineage", "n": n})

    stats = _timed(lambda: backend.save(ENVIRONMENT, snapshot))
    results["save"] = stats
    print(f"  Save {n} assets:                        {_fmt(stats)}")

    stats = _timed(lambda: backend.load(ENVIRONMENT))
    results["load"] = stats
    print(f"  Load {n} assets:                        {_fmt(stats)}")

    loaded = backend.load(ENVIRONMENT)
    assert loaded is not None and len(loaded.assets) == n
    backend.close()
    return results


# ── Main ─────────────────────────────────────────────────────

def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(description="Column lineage benchmark")
    parser.add_argument("--scale", type=int, default=DEFAULT_SCALE,
                        help="Scale factor (1=50 models, 10=500 models)")
    args = parser.parse_args()
    scale = args.scale

    n_total = 50 * scale
    print("=" * 70)
    print(f"  BENCHMARK: Column Lineage with sqlglot.lineage() — {n_total} models")
    print(f"  (scale={scale}, {{ ref() }} + CTE-heavy SQL)")
    print("=" * 70)

    tmp_root = Path(tempfile.mkdtemp(prefix="assets_lineage_bench_"))
    print(f"  Temp dir: {tmp_root}")
    all_results: dict[str, Any] = {}

    # Step 1: Generate DAG
    print(f"\n  Generating {n_total}-model DAG with CTE SQL...")
    t0 = time.perf_counter()
    models, schema = generate_dag(scale=scale)
    gen_ms = (time.perf_counter() - t0) * 1000
    layers = {}
    for m in models:
        layers[m.layer] = layers.get(m.layer, 0) + 1
    layer_str = " ".join(f"{k}={v}" for k, v in sorted(layers.items()))
    print(f"  DAG generated in {gen_ms:.1f}ms  ({layer_str})")

    # Show sample SQL
    for m in models:
        if m.layer == "mart" and m.jinja_sql:
            print(f"\n  Sample mart SQL ({m.name}):")
            for line in m.jinja_sql.split("\n"):
                print(f"    {line}")
            break

    # Step 2: Resolve refs and extract lineage
    print(f"\n  Resolving refs and extracting lineage with sqlglot.lineage()...")
    t0 = time.perf_counter()
    for m in models:
        extract_lineage_for_model(m, schema)
        if m.all_columns:
            schema[m.name] = {c: v.get("type", "VARCHAR") for c, v in m.all_columns.items()}
    lineage_ms = (time.perf_counter() - t0) * 1000

    total_cols = sum(len(m.all_columns) for m in models)
    defined_cols = sum(len(m.defined_columns) for m in models)
    discovered_cols = total_cols - defined_cols
    total_edges = sum(len(m.column_lineage) for m in models)
    total_refs = sum(len(m.depends_on) for m in models if m.jinja_sql)
    print(f"  Lineage extracted in {lineage_ms:.1f}ms")
    print(f"  Refs resolved: {total_refs}")
    print(f"  Columns: {total_cols} (defined={defined_cols}, discovered={discovered_cols})")
    print(f"  Lineage edges: {total_edges}")
    all_results["generation"] = {
        "dag_ms": gen_ms, "lineage_ms": lineage_ms,
        "total_cols": total_cols, "lineage_edges": total_edges,
        "refs": total_refs,
    }

    # Step 3: Store in SQLite
    db_path = tmp_root / "lineage.db"
    t0 = time.perf_counter()
    conn = create_lineage_db(db_path)
    store_lineage(conn, models)
    store_ms = (time.perf_counter() - t0) * 1000
    print(f"\n  Stored in SQLite in {store_ms:.1f}ms")
    all_results["storage_ms"] = store_ms

    mc = conn.execute("SELECT COUNT(*) FROM models").fetchone()[0]
    cc = conn.execute("SELECT COUNT(*) FROM columns").fetchone()[0]
    lc = conn.execute("SELECT COUNT(*) FROM column_lineage").fetchone()[0]
    dc = conn.execute("SELECT COUNT(*) FROM model_dependencies").fetchone()[0]
    print(f"  DB: {mc} models, {cc} columns, {lc} lineage edges, {dc} dep edges")

    # Step 4-5: Benchmark queries
    all_results["simple"] = bench_simple_queries(conn)
    all_results["complex"] = bench_downstream_queries(conn)
    all_results["backend"] = bench_state_backend(models, tmp_root)
    conn.close()

    # Summary
    print(f"\n{'=' * 70}")
    print("  SUMMARY")
    print(f"{'=' * 70}")
    print(f"\n  DAG generation:         {gen_ms:>10.1f} ms")
    print(f"  Lineage (sqlglot):      {lineage_ms:>10.1f} ms")
    print(f"  SQLite storage:         {store_ms:>10.1f} ms")
    print(f"  Refs resolved:          {total_refs:>10d}")
    print(f"  Total columns:          {total_cols:>10d}")
    print(f"  Lineage edges:          {total_edges:>10d}")

    for section, label in [("simple", "Simple Query"), ("complex", "Complex Query"),
                           ("backend", "State Backend")]:
        print(f"\n  {label:<35} {'Median ms':>10}")
        print(f"  {'─' * 45}")
        for k, v in all_results[section].items():
            print(f"  {k:<35} {v['median_ms']:>10.3f}")

    print()
    shutil.rmtree(tmp_root, ignore_errors=True)
    print(f"  Cleaned up {tmp_root}")


if __name__ == "__main__":
    main()
