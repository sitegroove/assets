#!/usr/bin/env python3
"""Demo 8: Fake Google Cloud resources as Python config files.

This is a Terraform-style setup where each resource is declared in its own
Python module as a Pydantic object. Resource files are configuration only:
they do not create cloud resources.

The demo loads from **two independent roots** to show the ``group``
feature of :class:`FileDiscovery`:

- ``project/``      — project-specific GCP resources
- ``shared_infra/`` — shared infrastructure module
  (simulates a vendored package installed at a different location)

The consumer-provided ``ResourcesLoader`` handles Python module imports,
``sys.path`` management, and module-cache cleanup.  The library handles
discovery, index diffing, state rehydration, registration, and deletions
via :meth:`FileDiscovery.load`.

Run:
    python demos/08_infra_as_code/main.py
"""

from __future__ import annotations

import shutil
from pathlib import Path

from loader import ResourcesLoader

from assets import (
    Project,
    SourceGroup,
)


def main() -> None:
    demo_root = Path(__file__).parent
    project_root = demo_root / "project"
    infra_root = demo_root / "shared_infra"
    state_dir = demo_root / ".assets_state"
    shutil.rmtree(state_dir, ignore_errors=True)

    project = Project(environment="production", state_dir=str(state_dir))

    groups = [
        SourceGroup(
            name="shared_infra",
            root=infra_root,
            directory=infra_root / "resources",
            patterns=["*.py"],
            exclude=["__init__.py"],
            loader=ResourcesLoader(
                deps=["infra_models.py", "infra_defaults.py"],
            ),
        ),
        SourceGroup(
            name="project_resources",
            root=project_root,
            directory=project_root / "resources",
            patterns=["*.py"],
            exclude=["__init__.py"],
            loader=ResourcesLoader(
                deps=["resource_models.py", "platform_defaults.py"],
            ),
        ),
    ]

    print("=" * 70)
    print("Fake Google Cloud Resources (Multi-Root Demo)")
    print("=" * 70)
    print(f"Project root:  {project_root}")
    print(f"Infra root:    {infra_root}")

    # -- First load --------------------------------------------------------
    print("\n[1] First load (parse all config files from both roots)")
    result = project.load(groups)
    print(result.summary())

    # -- Plan/apply --------------------------------------------------------
    print("\n[2] Plan/apply")
    plan = project.plan()
    print(plan.show())
    apply_result = project.apply(plan)
    print(f"Applied: created={apply_result.created}, updated={apply_result.updated}")

    # -- Second load (cache) -----------------------------------------------
    print("\n[3] Second load (cache + state fast path)")
    project.clear()
    result2 = project.load(groups)
    print(result2.summary())

    # -- Lineage -----------------------------------------------------------
    print("\n[4] Lineage")
    graph = project.graph
    print(f"Roots:  {sorted(graph.roots())}")
    print(f"Leaves: {sorted(graph.leaves())}")
    print(f"Impacted by network change: {sorted(graph.stale({'shared-vpc'}))}")
    print(f"Impacted by logging change: {sorted(graph.stale({'central-logs'}))}")


if __name__ == "__main__":
    main()
