#!/usr/bin/env python3
"""Demo 9: Fake dbt-style data transformation project.

This demo builds a dbt-like analytics project powered by the ``assets``
library.  It uses **Jinja2** for SQL templating (``{{ ref() }}``,
``{{ source() }}``, ``{{ var() }}``, custom macros) and **sqlglot** for
column-level lineage extraction.

Key features demonstrated:

- **External modules**: a ``modules/`` package simulates a dbt package
  (like ``dbt-google-ads``) loaded as a separate ``SourceGroup`` root.
- **Multi-layer architecture**: sources -> staging -> intermediate ->
  mart -> exposures.
- **Jinja2 rendering**: ``ref()``, ``source()``, ``var()``, and custom
  SQL macros (``safe_divide``, ``cents_to_dollars``).
- **sqlglot column lineage**: column-level dependency resolution
  through CTEs, JOINs, and aggregations.
- **Cross-module references**: ``mart_account_360`` in the project
  depends on ``mart_ad_performance`` from the external module.
- **Full asset lifecycle**: registry, plan/apply, graph exploration,
  selectors, impact analysis, PII scanning, test and metric catalogues.

Run:
    python demos/09_data_transformation/main.py
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import yaml

from assets import (
    Project,
)

# Add demo directory to sys.path so local imports resolve
_DEMO_DIR = Path(__file__).resolve().parent
if str(_DEMO_DIR) not in sys.path:
    sys.path.insert(0, str(_DEMO_DIR))

from loader import ExposureLoader, ModelLoader, SourceLoader  # noqa: E402
from models import Classification  # noqa: E402
from utils import ColumnLineageResolver, JinjaRenderer  # noqa: E402


# ── Helpers ───────────────────────────────────────────────────────────


def _load_variables(*paths: Path) -> dict[str, object]:
    """Merge variable files (later files win)."""
    merged: dict[str, object] = {}
    for p in paths:
        if p.exists():
            data = yaml.safe_load(p.read_text())
            if isinstance(data, dict):
                merged.update(data)
    return merged


def _load_sources(
    *source_dirs: Path,
) -> tuple[SourceLoader, list[object]]:
    """Pre-load all source YAML files and return the loader + assets."""
    loader = SourceLoader()
    all_assets = []
    for d in source_dirs:
        if not d.is_dir():
            continue
        for yml in sorted(d.glob("*.yml")):
            loaded = loader.load(yml, d)
            all_assets.extend(loaded)
    return loader, all_assets


def _print_section(number: int, title: str) -> None:
    print(f"\n{'=' * 70}")
    print(f"[{number}] {title}")
    print("=" * 70)


# ── Main ──────────────────────────────────────────────────────────────


def main() -> None:
    demo_root = _DEMO_DIR
    project_root = demo_root / "project"
    module_root = demo_root / "modules"
    state_dir = demo_root / ".assets_state"

    # Clean previous state
    shutil.rmtree(state_dir, ignore_errors=True)

    # ── Step 1: Setup ─────────────────────────────────────────────────
    _print_section(1, "Setup: Registry, StateManager, Variables")

    project = Project(environment="production", state_dir=str(state_dir))
    registry = project.registry

    # Load variables from both roots (module vars first, project wins)
    variables = _load_variables(
        module_root / "variables.yml",
        project_root / "variables.yml",
    )
    print(f"  Variables: {variables}")

    # ── Step 2: Load sources ──────────────────────────────────────────
    _print_section(2, "Load Sources (Salesforce + Google Ads)")

    source_loader, source_assets = _load_sources(
        module_root / "sources",
        project_root / "sources",
    )
    source_mapping = source_loader.source_mapping
    print(f"  Source mapping: {source_mapping}")

    # Register source assets (batched)
    registry.register_many([la.asset for la in source_assets])

    print(f"  Registered {len(source_assets)} source tables:")
    for la in source_assets:
        cols = [c.id for c in la.asset.children()]
        print(f"    {la.asset.id} ({la.asset.type}) -> columns: {cols}")

    # ── Step 3: Load module models ────────────────────────────────────
    _print_section(3, "Load Module Models (Google Ads package)")

    # Build renderer with source mapping, variables, and project macros
    macro_paths = [
        project_root / "macros",
    ]
    renderer = JinjaRenderer(
        source_mapping=source_mapping,
        variables=variables,
        macro_paths=macro_paths,
    )
    model_loader = ModelLoader(renderer)

    module_models = []
    for yml in sorted((module_root / "models").rglob("*.yml")):
        loaded = model_loader.load(yml, module_root)
        module_models.extend(loaded)
    registry.register_many([la.asset for la in module_models])
    for la in module_models:
        print(
            f"  {la.asset.id} ({la.asset.type}, "
            f"materialized={la.asset.materialized}) "
            f"depends_on={la.asset.depends_on}"
        )
    print(f"  -> {len(module_models)} module models loaded")

    # ── Step 4: Load project models ───────────────────────────────────
    _print_section(4, "Load Project Models (Salesforce analytics)")

    project_models = []
    for yml in sorted((project_root / "models").rglob("*.yml")):
        loaded = model_loader.load(yml, project_root)
        project_models.extend(loaded)
    registry.register_many([la.asset for la in project_models])
    for la in project_models:
        print(
            f"  {la.asset.id} ({la.asset.type}, "
            f"materialized={la.asset.materialized}) "
            f"depends_on={la.asset.depends_on}"
        )
    print(f"  -> {len(project_models)} project models loaded")

    # ── Step 5: Load exposures ────────────────────────────────────────
    _print_section(5, "Load Exposures (Dashboards & Reports)")

    exposure_loader = ExposureLoader()
    exposure_assets = []
    exposures_dir = project_root / "exposures"
    if exposures_dir.is_dir():
        for yml in sorted(exposures_dir.glob("*.yml")):
            loaded = exposure_loader.load(yml, project_root)
            exposure_assets.extend(loaded)
    if exposure_assets:
        registry.register_many([la.asset for la in exposure_assets])
    for la in exposure_assets:
        print(f"  {la.asset.id} ({la.asset.type}) depends_on={la.asset.depends_on}")
    print(f"  -> {len(exposure_assets)} exposures loaded")

    total = len(registry.all())
    print(f"\n  TOTAL ASSETS REGISTERED: {total}")

    # ── Step 6: Graph exploration ─────────────────────────────────────
    _print_section(6, "Graph Exploration")

    graph = registry.graph
    topo = graph.topological_sort()
    roots = sorted(graph.roots())
    leaves = sorted(graph.leaves())

    print(f"  Topological order: {topo}")
    print(f"  Roots (sources):   {roots}")
    print(f"  Leaves (terminal): {leaves}")

    # Downstream of a source
    downstream = sorted(graph.descendants("raw_accounts"))
    print(f"\n  Downstream of raw_accounts: {downstream}")

    # Upstream of a mart
    upstream = sorted(graph.ancestors("mart_account_360"))
    print(f"  Upstream of mart_account_360: {upstream}")

    # ── Step 7: Selectors ─────────────────────────────────────────────
    _print_section(7, "Selectors")

    selectors = [
        ("type:source", "All sources"),
        ("type:mart", "All marts"),
        ("type:exposure", "All exposures"),
        ("tag:pii", "PII-tagged assets"),
        ("tag:finance", "Finance-tagged assets"),
        ("tag:google_ads", "Google Ads assets"),
        ("+mart_sales_pipeline", "mart_sales_pipeline + all upstream"),
        ("raw_accounts+", "raw_accounts + all downstream"),
    ]
    for selector, desc in selectors:
        result = project.select(selector)
        print(f"  {selector:40s} -> {sorted(result.names)} ({desc})")

    # ── Step 8: Nested asset introspection ────────────────────────────
    _print_section(8, "Nested Asset Introspection (Columns)")

    stg_accounts = registry.get("stg_accounts")
    if stg_accounts:
        print(f"  stg_accounts children: {[c.id for c in stg_accounts.children()]}")

    # PII scan across all assets
    pii_fields: list[str] = []
    restricted_fields: list[str] = []
    for asset in registry.all():
        for child in asset.children():
            if hasattr(child, "pii") and child.pii:
                pii_fields.append(f"{asset.id}/{child.id}")
            if (
                hasattr(child, "classification")
                and child.classification == Classification.RESTRICTED
            ):
                restricted_fields.append(f"{asset.id}/{child.id}")

    print(f"\n  PII columns:        {pii_fields}")
    print(f"  RESTRICTED columns: {restricted_fields}")

    # ── Step 9: Column-level lineage via sqlglot ──────────────────────
    _print_section(9, "Column-Level Lineage (sqlglot)")

    registry.add_resolver("lineage", ColumnLineageResolver())
    lineage_targets = [
        "stg_accounts",
        "stg_contacts",
        "int_account_contacts",
        "mart_sales_pipeline",
        "mart_ad_performance",
    ]

    for target_id in lineage_targets:
        asset = registry.get(target_id)
        if not asset or not getattr(asset, "sql", None):
            continue
        print(f"\n  {target_id}:")
        mappings = registry.resolve("lineage", asset_id=target_id)
        if mappings:
            for fm in mappings:
                t = f" ({fm.transform})" if fm.transform else ""
                print(f"    {fm.target} <- {fm.source}{t}")
        else:
            print("    (no mappings resolved)")

    # ── Step 10: Plan & apply ─────────────────────────────────────────
    _print_section(10, "Plan & Apply (Production)")

    plan = project.plan()
    print(plan.show())

    if plan.has_changes:
        result = project.apply(plan)
        print(
            f"  Applied: created={result.created}, "
            f"updated={result.updated}, deleted={result.deleted}"
        )

    # Verify clean
    plan2 = project.plan()
    print(f"\n  Re-plan has changes: {plan2.has_changes}")

    # ── Step 11: Impact analysis ──────────────────────────────────────
    _print_section(11, "Impact Analysis")

    impacted_by_accounts = sorted(graph.stale({"raw_accounts"}))
    print(f"  Change raw_accounts -> impacted: {impacted_by_accounts}")

    impacted_by_ads = sorted(graph.stale({"raw_ad_performance"}))
    print(f"  Change raw_ad_performance -> impacted: {impacted_by_ads}")

    # Cross-module: changing a module source ripples into project marts
    impacted_by_campaigns = sorted(graph.stale({"raw_campaigns"}))
    print(f"  Change raw_campaigns -> impacted: {impacted_by_campaigns}")
    print(
        "  (Note: mart_account_360 is impacted via cross-module "
        "dependency through mart_ad_performance)"
    )

    # ── Step 12: Test catalogue ───────────────────────────────────────
    _print_section(12, "Data Quality Test Catalogue")

    for asset in registry.all():
        if not hasattr(asset, "tests"):
            continue
        tests = asset.tests
        if tests:
            print(f"  {asset.id}:")
            for t in tests:
                col = f" (column: {t.column})" if t.column else ""
                print(f"    - {t.name} [{t.type}]{col} severity={t.severity}")

    # ── Step 13: Metrics catalogue ────────────────────────────────────
    _print_section(13, "Business Metrics Catalogue")

    for asset in registry.all():
        if not hasattr(asset, "metrics"):
            continue
        metrics = asset.metrics
        if metrics:
            print(f"  {asset.id}:")
            for m in metrics:
                print(f"    - {m.name}: {m.expression} ({m.time_grain})")

    # ── Cleanup ───────────────────────────────────────────────────────
    manager = project.manager
    if manager is not None and hasattr(manager.backend, "close"):
        manager.backend.close()
    print(f"\n{'=' * 70}")
    print("Demo complete.")
    print("=" * 70)


if __name__ == "__main__":
    main()
