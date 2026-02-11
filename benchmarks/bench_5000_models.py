"""Benchmark suite — compilation pipeline at scale.

Focuses on what the *library* owns:
  1. Jinja2 SQL template compilation (consumer side, for realistic SQL)
  2. Registration & dependency resolution (ref extraction, cycle detection)
  3. Compiled cache (cold vs warm file loading)
  4. Graph construction & traversal
  5. Plan / apply / drift lifecycle
  6. Promotion between environments (with partial changes)
  7. Column lineage resolution
  8. Memory usage throughout

Configurable via NUM_MODELS (default 500, pass CLI arg to override).
"""

from __future__ import annotations

import gc
import json
import random
import resource
import shutil
import statistics
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

import jinja2
import sqlglot

# ── make sure the assets package is importable ──────────────────────────
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pydantic import BaseModel

from assets import (
    Asset,
    AssetField,
    FieldMapping,
    LineageResolver,
    MemoryBackend,
    Registry,
)
from assets.engine.differ import Differ
from assets.engine.manager import StateManager
from assets.loader.project import ProjectLoader
from assets.state.environment import Environment, EnvironmentConfig

# ── Constants ───────────────────────────────────────────────────────────
NUM_MODELS = int(sys.argv[1]) if len(sys.argv) > 1 else 500
SEED = 42
MAX_DEPS_PER_MODEL = 4
NUM_COLUMNS = 6

random.seed(SEED)


# ── Column / DataModel ──────────────────────────────────────────────────

class Column(BaseModel):
    name: str
    type: str = "string"


class DataModel(Asset):
    columns: list[Column] = AssetField(default_factory=list, field_source=True)


# ── Jinja2 SQL compilation ──────────────────────────────────────────────

JINJA_ENV = jinja2.Environment(
    loader=jinja2.BaseLoader(),
    undefined=jinja2.StrictUndefined,
)

SQL_TEMPLATE = JINJA_ENV.from_string("""\
SELECT
{%- for col in columns %}
    {{ col.expr }} AS {{ col.name }}{{ "," if not loop.last }}
{%- endfor %}
FROM {{ ref(deps[0]) }}
{%- for dep in deps[1:] %}
LEFT JOIN {{ ref(dep) }} ON {{ ref(deps[0]) }}.id = {{ ref(dep) }}.id
{%- endfor %}
""")

COLUMN_TYPES = ["string", "integer", "float", "boolean", "timestamp"]


def _ref(name: str) -> str:
    return "{{ ref('" + name + "') }}"


def _random_columns(dep_names: list[str]) -> list[dict]:
    cols = [{"name": "id", "expr": f"{dep_names[0]}.id", "type": "integer"}]
    for i in range(1, NUM_COLUMNS):
        dep = random.choice(dep_names)
        cols.append({
            "name": f"col_{i}",
            "expr": f"{dep}.col_{i}",
            "type": random.choice(COLUMN_TYPES),
        })
    return cols


# ── sqlglot-based LineageResolver ────────────────────────────────────────

class SqlglotLineageResolver(LineageResolver):
    """Consumer-side lineage resolver using sqlglot."""

    def resolve(self, sql: str, schema: dict[str, list[str]]) -> list[FieldMapping]:
        mappings: list[FieldMapping] = []
        try:
            parsed = sqlglot.parse_one(sql)
        except sqlglot.errors.ParseError:
            return mappings
        for expr in parsed.find_all(sqlglot.exp.Column):
            col_name = expr.name
            table = expr.table if expr.table else None
            source = None
            if table and table in schema:
                source = table
            else:
                for asset_name, cols in schema.items():
                    if col_name in cols:
                        source = asset_name
                        break
            if source:
                mappings.append(FieldMapping(
                    source_asset=source, source_field=col_name,
                    target_asset="__target__", target_field=col_name,
                ))
        return mappings


# ── Model generation ─────────────────────────────────────────────────────

def _distribute_layers(total: int) -> list[tuple[str, int]]:
    """Split total models across layers proportionally."""
    n_sources = max(20, total // 10)
    remaining = total - n_sources
    layers = [
        ("staging", 0.30),
        ("intermediate", 0.30),
        ("mart", 0.25),
        ("reporting", 0.15),
    ]
    result = [("raw", n_sources)]
    allocated = 0
    for i, (name, pct) in enumerate(layers):
        if i == len(layers) - 1:
            count = remaining - allocated
        else:
            count = int(remaining * pct)
        result.append((name, count))
        allocated += count
    return result


def generate_models(n: int) -> tuple[list[dict[str, Any]], float]:
    """Generate n model dicts with Jinja2-compiled SQL."""
    t0 = time.perf_counter()
    models: list[dict[str, Any]] = []
    all_available: list[str] = []

    for layer_name, count in _distribute_layers(n):
        for i in range(count):
            name = f"{layer_name}.model_{i}"

            if layer_name == "raw":
                # Source tables — no SQL
                cols = [Column(name="id", type="integer")]
                cols += [Column(name=f"col_{j}", type=random.choice(COLUMN_TYPES))
                         for j in range(1, NUM_COLUMNS)]
                models.append({
                    "name": name,
                    "kind": "source",
                    "tags": ["raw", f"group_{random.randint(0, 4)}"],
                    "columns": [c.model_dump() for c in cols],
                })
            else:
                # Models with SQL — pick deps from available pool
                n_deps = min(random.randint(1, MAX_DEPS_PER_MODEL), len(all_available))
                deps = random.sample(all_available, n_deps)
                col_defs = _random_columns(deps)

                # Compile SQL with Jinja2
                sql = SQL_TEMPLATE.render(columns=col_defs, deps=deps, ref=_ref)

                cols = [Column(name=c["name"], type=c["type"]) for c in col_defs]
                models.append({
                    "name": name,
                    "kind": "model",
                    "sql": sql,
                    "tags": [layer_name, f"group_{random.randint(0, 9)}"],
                    "description": f"{layer_name} model {i}",
                    "columns": [c.model_dump() for c in cols],
                })

            all_available.append(name)

    return models, time.perf_counter() - t0


# ── Helpers ──────────────────────────────────────────────────────────────

def _mem_mb() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024


def _time_it(fn):
    gc.collect()
    t0 = time.perf_counter()
    result = fn()
    return result, time.perf_counter() - t0


def _multi_time(fn, n=10):
    times = []
    result = None
    for _ in range(n):
        gc.collect()
        t0 = time.perf_counter()
        result = fn()
        times.append(time.perf_counter() - t0)
    return result, times


def _write_models_to_dir(model_dicts: list[dict], tmpdir: str) -> None:
    for md in model_dicts:
        p = Path(tmpdir) / f"{md['name'].replace('.', '_')}.json"
        p.write_text(json.dumps(md))


def _fmt_ms(seconds: float) -> str:
    if seconds < 0.001:
        return f"{seconds * 1_000_000:.0f}us"
    if seconds < 1.0:
        return f"{seconds * 1000:.2f}ms"
    return f"{seconds:.3f}s"


# ── Benchmark runner ─────────────────────────────────────────────────────

def run() -> None:
    sep = "=" * 72
    print(sep)
    print(f"  BENCHMARK: {NUM_MODELS:,} models")
    print(sep)
    print()

    mem_start = _mem_mb()

    # ── 1. Jinja2 SQL compilation ────────────────────────────────────────
    print("1. JINJA2 SQL COMPILATION")
    print("-" * 60)
    model_dicts, gen_time = generate_models(NUM_MODELS)
    n_with_sql = sum(1 for m in model_dicts if m.get("sql"))
    print(f"   Total models:       {len(model_dicts):,}")
    print(f"   Models with SQL:    {n_with_sql:,}")
    print(f"   Compilation time:   {_fmt_ms(gen_time)}")
    print(f"   Per model:          {_fmt_ms(gen_time / len(model_dicts))}")
    mem_after_gen = _mem_mb()
    print(f"   Memory (RSS):       {mem_after_gen:.1f} MB (+{mem_after_gen - mem_start:.1f} MB)")
    print()

    # ── 2. Registration with cycle detection ─────────────────────────────
    print("2. REGISTRATION (ref extraction + cycle detection)")
    print("-" * 60)

    registry = Registry(validate_acyclic=True)
    t0 = time.perf_counter()
    for md in model_dicts:
        asset = DataModel.model_validate(md)
        registry.register(asset)
    reg_time = time.perf_counter() - t0

    print(f"   Assets:             {len(registry):,}")
    print(f"   Dependencies:       {len(registry.dependencies):,}")
    print(f"   Time:               {_fmt_ms(reg_time)}")
    print(f"   Per model:          {_fmt_ms(reg_time / len(model_dicts))}")
    mem_after_reg = _mem_mb()
    print(f"   Memory (RSS):       {mem_after_reg:.1f} MB (+{mem_after_reg - mem_after_gen:.1f} MB)")
    print()

    # ── 2b. Registration WITHOUT cycle detection ─────────────────────────
    print("2b. REGISTRATION (no cycle detection)")
    print("-" * 60)
    reg_no_cycle = Registry(validate_acyclic=False)
    t0 = time.perf_counter()
    for md in model_dicts:
        asset = DataModel.model_validate(md)
        reg_no_cycle.register(asset)
    reg_time_no = time.perf_counter() - t0
    speedup = reg_time / reg_time_no if reg_time_no > 0 else 0
    print(f"   Time:               {_fmt_ms(reg_time_no)}")
    print(f"   Per model:          {_fmt_ms(reg_time_no / len(model_dicts))}")
    print(f"   Cycle detection overhead: {speedup:.2f}x")
    print()

    # ── 3. Graph construction & topology ─────────────────────────────────
    print("3. GRAPH")
    print("-" * 60)
    graph, build_time = _time_it(lambda: registry.graph)
    print(f"   Build time:         {_fmt_ms(build_time)}")
    print(f"   Nodes:              {len(graph):,}")
    print(f"   Edges:              {len(graph.dependencies):,}")
    print(f"   Roots:              {len(graph.roots()):,}")
    print(f"   Leaves:             {len(graph.leaves()):,}")

    _, topo_times = _multi_time(graph.topological_sort, 10)
    print(f"   Topological sort:   {_fmt_ms(statistics.mean(topo_times))} (mean of 10)")
    print()

    # ── 4. Traversal ─────────────────────────────────────────────────────
    print("4. TRAVERSAL")
    print("-" * 60)
    roots = sorted(graph.roots())
    leaves = sorted(graph.leaves())

    root = roots[0]
    desc = graph.descendants(root)
    _, desc_times = _multi_time(lambda: graph.descendants(root), 20)
    print(f"   descendants('{root}'): {len(desc):,} nodes, {_fmt_ms(statistics.mean(desc_times))} avg")

    leaf = leaves[len(leaves) // 2]
    anc = graph.ancestors(leaf)
    _, anc_times = _multi_time(lambda: graph.ancestors(leaf), 20)
    print(f"   ancestors('{leaf}'): {len(anc):,} nodes, {_fmt_ms(statistics.mean(anc_times))} avg")

    all_names = list(registry.assets.keys())
    sample = random.sample(all_names, min(100, len(all_names)))
    t0 = time.perf_counter()
    for n in sample:
        graph.ancestors(n)
        graph.descendants(n)
    batch_time = time.perf_counter() - t0
    print(f"   Batch (100 nodes, both): {_fmt_ms(batch_time)} total, {_fmt_ms(batch_time / len(sample))} avg")
    print()

    # ── 5. Selectors ─────────────────────────────────────────────────────
    print("5. SELECTORS")
    print("-" * 60)
    selectors = [
        ("kind:source", "source tables"),
        ("tag:staging", "staging layer"),
        ("tag:mart", "mart layer"),
        ("raw.*", "wildcard"),
        ("staging.model_0+", "model + descendants"),
        ("+reporting.model_0", "model + ancestors"),
        ("tag:staging,kind:model", "intersection"),
    ]
    for sel, label in selectors:
        result = registry.select(sel)
        _, sel_times = _multi_time(lambda s=sel: registry.select(s), 10)
        print(f"   '{sel}' ({label}): {len(result.names):,} matched, {_fmt_ms(statistics.mean(sel_times))} avg")
    print()

    # ── 6. File loading — cold vs warm cache ─────────────────────────────
    print("6. FILE LOADING (cold vs warm compiled cache)")
    print("-" * 60)
    tmpdir = tempfile.mkdtemp(prefix="assets_bench_")
    cache_dir = f"{tmpdir}/.cache"
    try:
        _write_models_to_dir(model_dicts, tmpdir)

        # Cold load (no cache)
        cold_reg = Registry(validate_acyclic=False)
        cold_loader = ProjectLoader(cold_reg, asset_class=DataModel, cache_dir=cache_dir)
        cold_result, cold_time = _time_it(lambda: cold_loader.load(tmpdir))
        print(f"   Cold load:          {_fmt_ms(cold_time)} ({cold_result.loaded} loaded, "
              f"{cold_result.recompiled} compiled, {cold_result.reused} cached)")

        # Warm load (cache populated)
        warm_reg = Registry(validate_acyclic=False)
        warm_loader = ProjectLoader(warm_reg, asset_class=DataModel, cache_dir=cache_dir)
        warm_result, warm_time = _time_it(lambda: warm_loader.load(tmpdir))
        print(f"   Warm load:          {_fmt_ms(warm_time)} ({warm_result.loaded} loaded, "
              f"{warm_result.recompiled} compiled, {warm_result.reused} cached)")
        if cold_time > 0:
            print(f"   Cache speedup:      {cold_time / warm_time:.2f}x")

        # ── 7. Plan / Apply / Drift lifecycle ────────────────────────────
        print()
        print("7. PLAN / APPLY / DRIFT")
        print("-" * 60)

        backend = MemoryBackend()
        env_config = EnvironmentConfig(
            default="production",
            environments={
                "production": Environment(name="production"),
                "staging": Environment(name="staging", parent="production"),
                "dev": Environment(name="dev", parent="production", shallow=True),
            },
        )
        sm_reg = Registry(validate_acyclic=False)
        sm_loader = ProjectLoader(sm_reg, asset_class=DataModel, cache_dir=cache_dir)
        manager = StateManager(sm_reg, sm_loader, backend, env_config)

        # Initial plan (all creates)
        plan, plan_time = _time_it(lambda: manager.plan(tmpdir, environment="production"))
        print(f"   Plan (initial):     {_fmt_ms(plan_time)}  ({len(plan.changeset.asset_changes)} creates)")

        # Apply
        result, apply_time = _time_it(lambda: manager.apply(plan, environment="production"))
        print(f"   Apply:              {_fmt_ms(apply_time)}  (created={result.created})")

        # Drift check (should be 0 changes)
        drift, drift_time = _time_it(lambda: manager.plan(tmpdir, environment="production"))
        print(f"   Drift (no changes): {_fmt_ms(drift_time)}  ({len(drift.changeset.asset_changes)} changes)")

        # Verify state held up
        prod_state = backend.load("production")
        print(f"   State durability:   production has {len(prod_state.assets) if prod_state else 0} assets stored")

        # ── 8. Incremental change — modify 10% of models, re-plan ────────
        print()
        print("8. INCREMENTAL CHANGE (modify 10% of models)")
        print("-" * 60)

        n_to_change = max(1, len(model_dicts) // 10)
        changeable = [m for m in model_dicts if m.get("sql")]
        changed_models = random.sample(changeable, min(n_to_change, len(changeable)))
        for md in changed_models:
            md["description"] = md.get("description", "") + " [updated]"
            md["tags"] = md.get("tags", []) + ["changed"]
            p = Path(tmpdir) / f"{md['name'].replace('.', '_')}.json"
            p.write_text(json.dumps(md))

        inc_plan, inc_plan_time = _time_it(lambda: manager.plan(tmpdir, environment="production"))
        n_creates = sum(1 for c in inc_plan.changeset.asset_changes if c.action == "create")
        n_updates = sum(1 for c in inc_plan.changeset.asset_changes if c.action == "update")
        n_deletes = sum(1 for c in inc_plan.changeset.asset_changes if c.action == "delete")
        print(f"   Changed files:      {len(changed_models)}")
        print(f"   Plan time:          {_fmt_ms(inc_plan_time)}")
        print(f"   Detected:           {n_creates} creates, {n_updates} updates, {n_deletes} deletes")

        inc_result, inc_apply_time = _time_it(
            lambda: manager.apply(inc_plan, environment="production")
        )
        print(f"   Apply time:         {_fmt_ms(inc_apply_time)}")

        # Verify drift is clean after apply
        drift2, drift2_time = _time_it(lambda: manager.plan(tmpdir, environment="production"))
        print(f"   Post-apply drift:   {_fmt_ms(drift2_time)}  ({len(drift2.changeset.asset_changes)} changes)")

        # ── 9. Promotion ─────────────────────────────────────────────────
        print()
        print("9. PROMOTION (production -> staging -> dev)")
        print("-" * 60)

        # Promote production → staging
        promo_plan, promo_time = _time_it(lambda: manager.promote("production", "staging"))
        print(f"   Promote plan (prod->staging): {_fmt_ms(promo_time)}  "
              f"({len(promo_plan.changeset.asset_changes)} changes)")

        promo_result, promo_apply = _time_it(
            lambda: manager.apply(promo_plan, environment="staging")
        )
        print(f"   Promote apply:      {_fmt_ms(promo_apply)}  (created={promo_result.created})")

        # Dev (shallow) — inherits from production
        dev_plan, dev_time = _time_it(lambda: manager.plan(tmpdir, environment="dev"))
        print(f"   Dev plan (shallow): {_fmt_ms(dev_time)}  "
              f"({len(dev_plan.changeset.asset_changes)} changes)")

        # Apply to dev, then promote dev → staging (partial)
        dev_apply_result, dev_apply_time = _time_it(
            lambda: manager.apply(dev_plan, environment="dev")
        )
        print(f"   Dev apply:          {_fmt_ms(dev_apply_time)}")

        # Selective promote using a tag selector
        sel_promo, sel_promo_time = _time_it(
            lambda: manager.promote("production", "staging", selector="tag:staging")
        )
        print(f"   Selective promote (tag:staging): {_fmt_ms(sel_promo_time)}  "
              f"({len(sel_promo.changeset.asset_changes)} changes)")

        # State check across environments
        prod_s = backend.load("production")
        staging_s = backend.load("staging")
        dev_s = backend.load("dev")
        print(f"   State check:        prod={len(prod_s.assets) if prod_s else 0}, "
              f"staging={len(staging_s.assets) if staging_s else 0}, "
              f"dev={len(dev_s.assets) if dev_s else 0}")

    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
    print()

    # ── 10. Column lineage ───────────────────────────────────────────────
    print("10. COLUMN LINEAGE (sqlglot resolver)")
    print("-" * 60)
    resolver = SqlglotLineageResolver()
    models_with_sql = [a for a in registry.all() if a.sql]

    sample_model = models_with_sql[len(models_with_sql) // 2]
    single = registry.resolve_column_lineage(asset_name=sample_model.name, resolver=resolver)
    _, single_times = _multi_time(
        lambda: registry.resolve_column_lineage(asset_name=sample_model.name, resolver=resolver), 10
    )
    print(f"   Single model:       {_fmt_ms(statistics.mean(single_times))} avg, {len(single)} mappings")

    batch_n = min(50, len(models_with_sql))
    batch_sample = random.sample(models_with_sql, batch_n)
    t0 = time.perf_counter()
    total_mappings = 0
    for m in batch_sample:
        r = registry.resolve_column_lineage(asset_name=m.name, resolver=resolver)
        total_mappings += len(r)
    batch_time = time.perf_counter() - t0
    print(f"   Batch ({batch_n} models):  {_fmt_ms(batch_time)} total, "
          f"{_fmt_ms(batch_time / batch_n)} avg, {total_mappings:,} mappings")

    t0 = time.perf_counter()
    sel_lineage = registry.resolve_column_lineage(selector="tag:staging", resolver=resolver)
    sel_time = time.perf_counter() - t0
    print(f"   Selector 'tag:staging': {_fmt_ms(sel_time)}, {len(sel_lineage):,} mappings")
    print()

    # ── 11. Memory summary ───────────────────────────────────────────────
    print("11. MEMORY")
    print("-" * 60)
    mem_final = _mem_mb()
    print(f"   Start:              {mem_start:.1f} MB")
    print(f"   After compilation:  {mem_after_gen:.1f} MB (+{mem_after_gen - mem_start:.1f} MB)")
    print(f"   After registration: {mem_after_reg:.1f} MB (+{mem_after_reg - mem_after_gen:.1f} MB)")
    print(f"   Final:              {mem_final:.1f} MB")
    print(f"   Total growth:       {mem_final - mem_start:.1f} MB")
    print()

    # ── Summary ──────────────────────────────────────────────────────────
    print(sep)
    print("  SUMMARY")
    print(sep)
    rows = [
        ("Jinja2 compilation", gen_time, f"{len(model_dicts)} models"),
        ("Registration (cycle detection ON)", reg_time, f"{len(registry)} assets"),
        ("Registration (cycle detection OFF)", reg_time_no, f"{speedup:.1f}x overhead"),
        ("Graph build", build_time, f"{len(graph)} nodes"),
        ("Topological sort", statistics.mean(topo_times), ""),
        ("File load (cold)", cold_time, f"{cold_result.recompiled} compiled"),
        ("File load (warm cache)", warm_time, f"{warm_result.reused} cached"),
        ("Plan (initial, all creates)", plan_time, f"{len(plan.changeset.asset_changes)} changes"),
        ("Apply (initial)", apply_time, f"{result.created} created"),
        ("Drift (no changes)", drift_time, "0 changes"),
        ("Incremental plan (10% changed)", inc_plan_time, f"{n_updates} updates"),
        ("Incremental apply", inc_apply_time, ""),
        ("Promote (prod->staging)", promo_time + promo_apply, ""),
        ("Lineage (single model)", statistics.mean(single_times), f"{len(single)} mappings"),
        ("Lineage (batch {})".format(batch_n), batch_time, f"{total_mappings} mappings"),
        ("Memory growth", 0, f"{mem_final - mem_start:.1f} MB"),
    ]
    print(f"  {'Operation':<45} {'Time':>12}  {'Notes'}")
    print(f"  {'-'*45} {'-'*12}  {'-'*20}")
    for label, t, note in rows:
        if label == "Memory growth":
            print(f"  {label:<45} {'':>12}  {note}")
        else:
            print(f"  {label:<45} {_fmt_ms(t):>12}  {note}")
    print(sep)


if __name__ == "__main__":
    run()
