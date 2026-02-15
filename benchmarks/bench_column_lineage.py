"""Benchmark — Column lineage with sqlglot over realistic analytical models.

Modernized to use the assets library's core APIs:

- ``Asset`` with nested children for columns
- ``Assets`` facade for high-level registration and selection workflows
- ``JinjaRenderer`` (Demo 09) for ``{{ ref() }}`` template resolution
- ``ColumnLineageResolver`` (Demo 09) for sqlglot column-level lineage
- ``Registry`` + ``register_many()`` for batch registration
- ``AssetGraph`` for graph exploration (ancestors, descendants, stale)
- ``SQLiteBackend`` + ``StateSnapshot`` for state persistence

Run:
    python benchmarks/bench_column_lineage.py            # 50 models (quick)
    python benchmarks/bench_column_lineage.py --scale 10 # 500 models

Generates a multi-layer DAG with {{ ref('model') }} Jinja syntax, CTE-heavy
analytical SQL (dbt/sqlmesh-style), renders templates via Jinja2, extracts
column lineage with sqlglot, then benchmarks the full library lifecycle.
"""

from __future__ import annotations

import hashlib
import random
import shutil
import statistics
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Ensure the project root and demo utils are importable
_BENCH_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _BENCH_DIR.parent
sys.path.insert(0, str(_PROJECT_ROOT))
sys.path.insert(0, str(_PROJECT_ROOT / "demos" / "09_data_transformation"))

from utils import ColumnLineageResolver, JinjaRenderer

from assets import (
    Asset,
    Assets,
    FieldMapping,
    Registry,
    SQLiteBackend,
)
from assets.state.models import AssetState, DependencyState, StateSnapshot

# ── Configuration ─────────────────────────────────────────────
DEFAULT_SCALE = 1
ITERATIONS = 5
ENVIRONMENT = "production"
SEED = 42

# Column type mapping
COL_TYPES: dict[str, str] = {
    "id": "INT",
    "uuid": "VARCHAR",
    "external_id": "VARCHAR",
    "created_at": "TIMESTAMP",
    "updated_at": "TIMESTAMP",
    "deleted_at": "TIMESTAMP",
    "event_ts": "TIMESTAMP",
    "user_id": "INT",
    "username": "VARCHAR",
    "email": "VARCHAR",
    "first_name": "VARCHAR",
    "last_name": "VARCHAR",
    "phone": "VARCHAR",
    "country": "VARCHAR",
    "order_id": "INT",
    "amount": "DECIMAL",
    "currency": "VARCHAR",
    "status": "VARCHAR",
    "discount": "DECIMAL",
    "tax": "DECIMAL",
    "total": "DECIMAL",
    "product_id": "INT",
    "product_name": "VARCHAR",
    "category": "VARCHAR",
    "price": "DECIMAL",
    "sku": "VARCHAR",
    "brand": "VARCHAR",
    "region": "VARCHAR",
    "segment": "VARCHAR",
    "channel": "VARCHAR",
    "platform": "VARCHAR",
    "device_type": "VARCHAR",
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


# ── Intermediate model specification ──────────────────────────


@dataclass
class ModelSpec:
    """Intermediate specification for a generated model.

    Holds the DAG generation output before conversion to ``Asset``
    objects.  Keeps the Jinja SQL template for re-render benchmarks.
    """

    name: str
    layer: str
    description: str
    jinja_sql: str | None
    depends_on: list[str]
    output_columns: list[str]
    defined_columns: dict[str, dict[str, str]] = field(default_factory=dict)


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
) -> tuple[list[ModelSpec], dict[str, dict[str, str]]]:
    """Build a multi-layer DAG with CTE-heavy analytical SQL.

    scale=1 -> 50 models, scale=10 -> 500 models.

    Returns:
        Tuple of (model_specs, schema) where schema maps model names
        to ``{column_name: column_type}`` for sqlglot.
    """
    n_sources = 5 * scale
    n_staging = 10 * scale
    n_intermediate = 15 * scale
    n_marts = 15 * scale
    n_reports = 5 * scale

    rng = random.Random(seed)
    specs: list[ModelSpec] = []
    schema: dict[str, dict[str, str]] = {}

    def _register(
        spec: ModelSpec,
        output_cols: dict[str, dict[str, str]],
    ) -> None:
        specs.append(spec)
        schema[spec.name] = {
            c: v.get("type", "VARCHAR") for c, v in output_cols.items()
        }

    # ── Layer 0: Sources (raw tables, no SQL) ──
    for i in range(n_sources):
        name = f"raw_{i:03d}"
        cols = _pick_columns(rng, rng.randint(5, 10))
        col_defs = _make_col_defs(cols, rng, describe_frac=0.9)
        spec = ModelSpec(
            name=name,
            layer="source",
            description=f"Raw source table {i}",
            jinja_sql=None,
            depends_on=[],
            output_columns=list(col_defs.keys()),
            defined_columns=col_defs,
        )
        _register(spec, col_defs)

    source_names = [s.name for s in specs if s.layer == "source"]

    # ── Layer 1: Staging (CTE with filtering + cleaning) ──
    for i in range(n_staging):
        name = f"stg_{i:03d}"
        src = rng.choice(source_names)
        src_cols = list(schema[src].keys())
        selected = rng.sample(
            src_cols, min(rng.randint(3, len(src_cols)), len(src_cols))
        )
        if "id" not in selected:
            selected[0] = "id"

        cte_cols = ", ".join(f"src.{c}" for c in selected)
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

        col_defs = _make_col_defs(
            output_cols_list, rng, describe_frac=0.6, include_frac=0.6
        )
        all_col_defs = _make_col_defs(output_cols_list, rng, describe_frac=1.0)
        spec = ModelSpec(
            name=name,
            layer="staging",
            description=f"Staging from {src}",
            jinja_sql=jinja,
            depends_on=[src],
            output_columns=output_cols_list,
            defined_columns=col_defs,
        )
        _register(spec, all_col_defs)

    staging_names = [s.name for s in specs if s.layer == "staging"]

    # ── Layer 2: Intermediate (CTE with JOINs across staging) ──
    for i in range(n_intermediate):
        name = f"int_{i:03d}"
        n_deps = rng.randint(2, min(3, len(staging_names)))
        deps = rng.sample(staging_names, n_deps)
        base, *joins = deps

        base_cols = list(schema.get(base, {}).keys())
        if not base_cols:
            base_cols = ["id"]

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

        out_cols = list(base_cols[:5])
        for dep in joins:
            dep_cols = [c for c in list(schema.get(dep, {}).keys()) if c != "id"][:3]
            out_cols.extend(dep_cols)
        seen: set[str] = set()
        unique_out: list[str] = []
        for c in out_cols:
            if c not in seen:
                seen.add(c)
                unique_out.append(c)

        col_defs = _make_col_defs(unique_out, rng, describe_frac=0.5, include_frac=0.5)
        all_col_defs = _make_col_defs(unique_out, rng, describe_frac=1.0)
        spec = ModelSpec(
            name=name,
            layer="intermediate",
            description=f"Join of {', '.join(deps)}",
            jinja_sql=jinja,
            depends_on=deps,
            output_columns=unique_out,
            defined_columns=col_defs,
        )
        _register(spec, all_col_defs)

    int_names = [s.name for s in specs if s.layer == "intermediate"]

    # ── Layer 3: Marts (CTE with filtering -> aggregation) ──
    for i in range(n_marts):
        name = f"mart_{i:03d}"
        base = rng.choice(int_names)
        deps = [base]
        base_cols = list(schema.get(base, {}).keys())
        if not base_cols:
            base_cols = ["id"]

        group_candidates = [
            c
            for c in base_cols
            if c
            in (
                "country",
                "region",
                "segment",
                "category",
                "channel",
                "status",
                "platform",
            )
        ]
        group_cols = group_candidates[:2] if group_candidates else ["id"]
        agg_candidates = [
            c for c in base_cols if c in ("amount", "total", "price", "tax", "discount")
        ]
        agg_cols = agg_candidates[:2] if agg_candidates else []

        extra_cte = ""
        extra_join = ""
        if rng.random() < 0.4 and staging_names:
            extra = rng.choice(staging_names)
            deps.append(extra)
            extra_cte = (
                f"enrichment AS (\n    SELECT * FROM {{{{ ref('{extra}') }}}}\n),\n"
            )
            extra_join = "\n    LEFT JOIN enrichment e ON filtered.id = e.id"

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
        all_col_defs = _make_col_defs(out_cols, rng, describe_frac=1.0)
        spec = ModelSpec(
            name=name,
            layer="mart",
            description=f"Mart from {base}",
            jinja_sql=jinja,
            depends_on=deps,
            output_columns=out_cols,
            defined_columns=col_defs,
        )
        _register(spec, all_col_defs)

    mart_names = [s.name for s in specs if s.layer == "mart"]

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
        all_col_defs = _make_col_defs(out_cols, rng, describe_frac=1.0)
        spec = ModelSpec(
            name=name,
            layer="report",
            description=f"Report from {src}",
            jinja_sql=jinja,
            depends_on=deps,
            output_columns=out_cols,
            defined_columns=col_defs,
        )
        _register(spec, all_col_defs)

    return specs, schema


# ── Asset construction from specs ─────────────────────────────


def build_assets(
    specs: list[ModelSpec],
    renderer: JinjaRenderer,
) -> list[Asset]:
    """Create ``Asset`` objects from model specs, rendering Jinja SQL.

    Each asset gets:
    - Resolved SQL (via ``JinjaRenderer``)
    - Column children (from ``spec.output_columns``)
    - ``depends_on`` extracted from Jinja ``ref()`` calls
    """
    assets: list[Asset] = []
    for spec in specs:
        sql: str | None = None
        depends_on = list(spec.depends_on)

        if spec.jinja_sql:
            rendered = renderer.render(spec.jinja_sql)
            sql = rendered.sql
            depends_on = rendered.refs

        # Create column children as nested Asset objects
        children = [
            Asset(
                id=col_name,
                type=spec.defined_columns.get(col_name, {}).get("type", "VARCHAR"),
                description=spec.defined_columns.get(col_name, {}).get(
                    "description", ""
                ),
            )
            for col_name in spec.output_columns
        ]

        asset = Asset(
            id=spec.name,
            type=spec.layer,
            description=spec.description,
            sql=sql,
            depends_on=depends_on,
            children=children,
            tags=[spec.layer],
            metadata={"layer": spec.layer},
        )
        assets.append(asset)
    return assets


# ── Benchmark helpers ─────────────────────────────────────────


def _timed(fn: Any, iterations: int = ITERATIONS) -> dict[str, float]:
    times = []
    for _ in range(iterations):
        t0 = time.perf_counter()
        fn()
        times.append((time.perf_counter() - t0) * 1000)
    return {
        "min_ms": min(times),
        "max_ms": max(times),
        "mean_ms": statistics.mean(times),
        "median_ms": statistics.median(times),
        "stdev_ms": statistics.stdev(times) if len(times) > 1 else 0.0,
    }


def _fmt(stats: dict[str, float]) -> str:
    return (
        f"  mean={stats['mean_ms']:8.2f}ms"
        f"  median={stats['median_ms']:8.2f}ms"
        f"  min={stats['min_ms']:8.2f}ms"
        f"  max={stats['max_ms']:8.2f}ms"
        f"  stdev={stats['stdev_ms']:7.2f}ms"
    )


# ── Benchmark: Jinja2 rendering ───────────────────────────────


def bench_jinja_rendering(
    specs: list[ModelSpec],
    renderer: JinjaRenderer,
) -> dict[str, dict[str, float]]:
    """Benchmark Jinja2 template rendering."""
    print(f"\n{'=' * 70}")
    print("  Jinja2 Rendering")
    print(f"{'=' * 70}")
    results: dict[str, dict[str, float]] = {}

    sql_specs = [s for s in specs if s.jinja_sql]

    def _render_all() -> None:
        for s in sql_specs:
            renderer.render(s.jinja_sql)  # type: ignore[arg-type]

    stats = _timed(_render_all)
    results["render_all"] = stats
    print(f"  Render all ({len(sql_specs)} templates):          {_fmt(stats)}")

    sample = sql_specs[len(sql_specs) // 2]

    stats = _timed(lambda: renderer.render(sample.jinja_sql))  # type: ignore[arg-type]
    results["render_single"] = stats
    print(f"  Render single ({sample.name}):            {_fmt(stats)}")

    return results


# ── Benchmark: Column lineage extraction ──────────────────────


def bench_lineage_extraction(
    registry: Registry,
    resolver: ColumnLineageResolver,
) -> tuple[dict[str, dict[str, float]], dict[str, list[FieldMapping]]]:
    """Benchmark sqlglot column lineage via ColumnLineageResolver."""
    print(f"\n{'=' * 70}")
    print("  Column Lineage Extraction (sqlglot)")
    print(f"{'=' * 70}")
    results: dict[str, dict[str, float]] = {}

    sql_assets = [a for a in registry.all() if a.sql]
    all_lineage: dict[str, list[FieldMapping]] = {}

    def _extract_all() -> None:
        all_lineage.clear()
        for asset in sql_assets:
            mappings = registry.resolve_field_dependency(
                asset_id=asset.id, resolver=resolver
            )
            all_lineage[asset.id] = mappings

    # Slow operation — only 2 iterations
    stats = _timed(_extract_all, iterations=2)
    results["extract_all"] = stats

    total_mappings = sum(len(m) for m in all_lineage.values())
    total_cols = sum(len(a.children) for a in registry.all())
    print(f"  Extract all ({len(sql_assets)} SQL models):            {_fmt(stats)}")
    print(f"    -> {total_cols} columns, {total_mappings} lineage edges")

    # Single model extraction
    sample_id = next(
        (a.id for a in sql_assets if a.type == "mart"),
        sql_assets[0].id,
    )

    stats = _timed(
        lambda: registry.resolve_field_dependency(asset_id=sample_id, resolver=resolver)
    )
    results["extract_single"] = stats
    n = len(all_lineage.get(sample_id, []))
    print(f"  Extract single ({sample_id}, {n} edges):    {_fmt(stats)}")

    return results, all_lineage


# ── Benchmark: Registry & graph operations ────────────────────


def bench_registry_graph(
    assets: list[Asset],
) -> dict[str, dict[str, float]]:
    """Benchmark registry registration and AssetGraph operations."""
    n = len(assets)
    print(f"\n{'=' * 70}")
    print(f"  Registry & Graph -- {n} Assets")
    print(f"{'=' * 70}")
    results: dict[str, dict[str, float]] = {}

    # register_many
    def _register() -> Assets:
        project = Assets()
        project.register_many(assets)
        return project

    stats = _timed(_register)
    results["register_many"] = stats
    print(f"  register_many ({n}):              {_fmt(stats)}")

    project = _register()

    # Graph build (force rebuild each iteration)
    def _build_graph() -> None:
        project.registry._graph = None  # noqa: SLF001
        _ = project.graph

    stats = _timed(_build_graph)
    results["graph_build"] = stats
    print(f"  Graph build:                        {_fmt(stats)}")

    graph = project.graph

    # Topological sort
    stats = _timed(lambda: graph.topological_sort())
    results["topo_sort"] = stats
    print(f"  Topological sort:                   {_fmt(stats)}")

    # Roots and leaves
    roots = sorted(graph.roots())
    stats = _timed(lambda: graph.roots())
    results["roots"] = stats
    print(f"  Roots ({len(roots)}):                       {_fmt(stats)}")

    leaves = sorted(graph.leaves())
    stats = _timed(lambda: graph.leaves())
    results["leaves"] = stats
    print(f"  Leaves ({len(leaves)}):                      {_fmt(stats)}")

    # Ancestors (upstream trace)
    leaf = leaves[0] if leaves else assets[-1].id
    anc = graph.ancestors(leaf)
    stats = _timed(lambda: graph.ancestors(leaf))
    results["ancestors"] = stats
    print(f"  Ancestors of {leaf} ({len(anc)}):        {_fmt(stats)}")

    # Descendants (downstream trace)
    root = roots[0] if roots else assets[0].id
    desc = graph.descendants(root)
    stats = _timed(lambda: graph.descendants(root))
    results["descendants"] = stats
    print(f"  Descendants of {root} ({len(desc)}):      {_fmt(stats)}")

    # Stale (impact analysis)
    stale = graph.stale({root})
    stats = _timed(lambda: graph.stale({root}))
    results["stale"] = stats
    print(f"  Stale from {root} ({len(stale)}):          {_fmt(stats)}")

    # Selectors
    for selector in ["type:source", "type:mart", "tag:staging"]:
        sel_result = project.select(selector)
        stats = _timed(lambda s=selector: project.select(s))
        results[f"select_{selector}"] = stats
        print(f"  Select '{selector}' ({len(sel_result.names)}):        {_fmt(stats)}")

    return results


# ── Benchmark: State backend ─────────────────────────────────


def bench_state_backend(
    registry: Registry,
    lineage: dict[str, list[FieldMapping]],
    tmp_dir: Path,
) -> dict[str, dict[str, float]]:
    """Benchmark SQLiteBackend save/load with column + lineage metadata."""
    n = len(registry)
    print(f"\n{'=' * 70}")
    print(f"  SQLiteBackend -- {n} Assets with Column + Lineage Metadata")
    print(f"{'=' * 70}")
    results: dict[str, dict[str, float]] = {}

    backend = SQLiteBackend(db_path=tmp_dir / "state.db")

    # Build snapshot with correct AssetState field names (id, type)
    assets_state: dict[str, AssetState] = {}
    deps_state: list[DependencyState] = []

    for asset in registry.all():
        lineage_data = [
            {
                "source": m.source,
                "target": m.target,
                "transform": m.transform,
            }
            for m in lineage.get(asset.id, [])
        ]
        assets_state[asset.id] = AssetState(
            id=asset.id,
            type=asset.type,
            fingerprint=asset.fingerprint,
            data={
                "description": asset.description,
                "sql": asset.sql,
                "columns": [c.id for c in asset.children],
                "lineage": lineage_data,
            },
            applied_by="benchmark",
            version=1,
        )
        for dep in asset.depends_on:
            deps_state.append(
                DependencyState(
                    source=dep,
                    target=asset.id,
                    type="ref",
                    fingerprint=hashlib.sha256(
                        f"{dep}:{asset.id}".encode()
                    ).hexdigest(),
                )
            )

    snapshot = StateSnapshot(
        environment=ENVIRONMENT,
        assets=assets_state,
        dependencies=deps_state,
        metadata={"benchmark": "column_lineage", "n": n},
    )

    # Save
    stats = _timed(lambda: backend.save(ENVIRONMENT, snapshot))
    results["save"] = stats
    print(f"  Save {n} assets:                    {_fmt(stats)}")

    # Load
    stats = _timed(lambda: backend.load(ENVIRONMENT))
    results["load"] = stats
    print(f"  Load {n} assets:                    {_fmt(stats)}")

    # Verify round-trip
    loaded = backend.load(ENVIRONMENT)
    assert loaded is not None and len(loaded.assets) == n

    # Incremental save (30 changed assets)
    changed_ids = set(list(assets_state.keys())[:30])
    stats = _timed(lambda: backend.save(ENVIRONMENT, snapshot, changed_ids=changed_ids))
    results["save_incremental_30"] = stats
    print(f"  Save incremental (30 assets):       {_fmt(stats)}")

    backend.close()
    return results


# ── Benchmark: Cache vs reparse ───────────────────────────────


def bench_cache_vs_reparse(
    registry: Registry,
    resolver: ColumnLineageResolver,
    tmp_dir: Path,
) -> dict[str, dict[str, float]]:
    """Compare loading lineage from SQLite state vs re-parsing with sqlglot."""
    print(f"\n{'=' * 70}")
    print("  Cache Hit vs Re-parse (sqlglot.lineage())")
    print(f"{'=' * 70}")
    results: dict[str, dict[str, float]] = {}

    backend = SQLiteBackend(db_path=tmp_dir / "state.db")

    # Cache hit: load full snapshot from state backend
    stats = _timed(lambda: backend.load(ENVIRONMENT))
    results["cache_load"] = stats
    loaded = backend.load(ENVIRONMENT)
    if loaded:
        n_assets = len(loaded.assets)
        n_lineage = sum(len(a.data.get("lineage", [])) for a in loaded.assets.values())
        print(f"  Cache load (state backend):         {_fmt(stats)}")
        print(f"    -> {n_assets} assets, {n_lineage} lineage edges from SQLite")

    # Re-parse: re-extract all lineage with sqlglot
    sql_assets = [a for a in registry.all() if a.sql]

    def _reparse_all() -> None:
        for asset in sql_assets:
            registry.resolve_field_dependency(asset_id=asset.id, resolver=resolver)

    stats = _timed(_reparse_all, iterations=2)
    results["reparse_all"] = stats
    print(f"  Re-parse (sqlglot, {len(sql_assets)} models):       {_fmt(stats)}")

    # Single model re-parse
    sample_id = next(
        (a.id for a in sql_assets if a.type == "mart"),
        sql_assets[0].id,
    )

    def _reparse_single() -> None:
        registry.resolve_field_dependency(asset_id=sample_id, resolver=resolver)

    stats = _timed(_reparse_single)
    results["reparse_single"] = stats
    print(f"  Re-parse single ({sample_id}):        {_fmt(stats)}")

    # Speedup
    cache_ms = results["cache_load"]["median_ms"]
    parse_ms = results["reparse_all"]["median_ms"]
    if cache_ms > 0:
        speedup = parse_ms / cache_ms
        print(
            f"\n  Speedup: {speedup:.0f}x"
            f"  (cache={cache_ms:.2f}ms vs parse={parse_ms:.1f}ms)"
        )

    backend.close()
    return results


# ── Main ──────────────────────────────────────────────────────


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(
        description="Column lineage benchmark (modernized)"
    )
    parser.add_argument(
        "--scale",
        type=int,
        default=DEFAULT_SCALE,
        help="Scale factor (1=50 models, 10=500 models)",
    )
    args = parser.parse_args()
    scale = args.scale

    n_total = 50 * scale
    print("=" * 70)
    print(f"  BENCHMARK: Column Lineage -- {n_total} models")
    print(f"  (scale={scale}, Jinja2 + sqlglot + full library lifecycle)")
    print("=" * 70)

    tmp_root = Path(tempfile.mkdtemp(prefix="assets_lineage_bench_"))
    print(f"  Temp dir: {tmp_root}")
    all_results: dict[str, Any] = {}

    # ── Step 1: Generate DAG ──
    print(f"\n  Generating {n_total}-model DAG with CTE SQL...")
    t0 = time.perf_counter()
    specs, schema = generate_dag(scale=scale)
    gen_ms = (time.perf_counter() - t0) * 1000
    layers: dict[str, int] = {}
    for s in specs:
        layers[s.layer] = layers.get(s.layer, 0) + 1
    layer_str = " ".join(f"{k}={v}" for k, v in sorted(layers.items()))
    print(f"  DAG generated in {gen_ms:.1f}ms  ({layer_str})")

    # Show sample SQL
    for s in specs:
        if s.layer == "mart" and s.jinja_sql:
            print(f"\n  Sample mart Jinja SQL ({s.name}):")
            for line in s.jinja_sql.split("\n"):
                print(f"    {line}")
            break

    # ── Step 2: Render Jinja + build Assets ──
    renderer = JinjaRenderer()

    print(f"\n  Rendering Jinja SQL and building {n_total} Asset objects...")
    t0 = time.perf_counter()
    assets = build_assets(specs, renderer)
    build_ms = (time.perf_counter() - t0) * 1000
    total_cols = sum(len(a.children) for a in assets)
    sql_count = sum(1 for a in assets if a.sql)
    refs_count = sum(len(a.depends_on) for a in assets if a.sql)
    print(
        f"  Built in {build_ms:.1f}ms"
        f"  ({total_cols} columns across {len(assets)} assets,"
        f" {sql_count} with SQL, {refs_count} refs)"
    )

    # Show sample resolved SQL
    for a in assets:
        if a.type == "mart" and a.sql:
            print(f"\n  Sample resolved SQL ({a.id}):")
            for line in a.sql.split("\n"):
                print(f"    {line}")
            break

    # ── Step 3: Register in Registry ──
    project = Assets()
    t0 = time.perf_counter()
    project.register_many(assets)
    register_ms = (time.perf_counter() - t0) * 1000
    registry = project.registry
    print(f"\n  Registered {len(assets)} assets in {register_ms:.1f}ms")

    all_results["generation"] = {
        "dag_ms": gen_ms,
        "build_ms": build_ms,
        "register_ms": register_ms,
        "total_cols": total_cols,
        "refs": refs_count,
    }

    # ── Step 4: Extract lineage ──
    resolver = ColumnLineageResolver()
    lineage_results, all_lineage = bench_lineage_extraction(registry, resolver)
    all_results["lineage"] = lineage_results

    # Store lineage in asset metadata for state persistence
    for asset in registry.all():
        mappings = all_lineage.get(asset.id, [])
        asset.metadata["lineage"] = [
            {
                "source": m.source,
                "target": m.target,
                "transform": m.transform,
            }
            for m in mappings
        ]

    # ── Step 5: Jinja rendering benchmark ──
    all_results["jinja"] = bench_jinja_rendering(specs, renderer)

    # ── Step 6: Registry & graph benchmark ──
    all_results["graph"] = bench_registry_graph(assets)

    # ── Step 7: State backend benchmark ──
    all_results["backend"] = bench_state_backend(registry, all_lineage, tmp_root)

    # ── Step 8: Cache vs reparse ──
    all_results["cache"] = bench_cache_vs_reparse(registry, resolver, tmp_root)

    # ── Summary ──
    total_edges = sum(len(m) for m in all_lineage.values())

    print(f"\n{'=' * 70}")
    print("  SUMMARY")
    print(f"{'=' * 70}")
    print(f"\n  DAG generation:         {gen_ms:>10.1f} ms")
    print(f"  Asset build + render:   {build_ms:>10.1f} ms")
    print(f"  Registry registration:  {register_ms:>10.1f} ms")
    print(f"  Refs resolved:          {refs_count:>10d}")
    print(f"  Total columns:          {total_cols:>10d}")
    print(f"  Lineage edges:          {total_edges:>10d}")

    for section, label in [
        ("lineage", "Lineage Extraction"),
        ("jinja", "Jinja Rendering"),
        ("graph", "Registry & Graph"),
        ("backend", "State Backend"),
        ("cache", "Cache vs Re-parse"),
    ]:
        print(f"\n  {label:<35} {'Median ms':>10}")
        print(f"  {'=' * 45}")
        for k, v in all_results[section].items():
            print(f"  {k:<35} {v['median_ms']:>10.3f}")

    print()
    shutil.rmtree(tmp_root, ignore_errors=True)
    print(f"  Cleaned up {tmp_root}")


if __name__ == "__main__":
    main()
