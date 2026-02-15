"""Smoke tests to keep demo workflows covered by CI.

These tests run each demo script end-to-end and assert key output markers.
They guard against regressions where library changes break consumer-facing
demo scenarios.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]


def _has_module(name: str) -> bool:
    return importlib.util.find_spec(name) is not None


@pytest.mark.parametrize(
    ("script", "required_modules", "must_contain"),
    [
        (
            "demos/01_core_basics/main.py",
            [],
            "Done!",
        ),
        (
            "demos/02_plan_apply_workflow/main.py",
            [],
            "Final Plan (clean)",
        ),
        (
            "demos/03_multi_environment/main.py",
            [],
            "Promote staging -> production",
        ),
        (
            "demos/04_custom_loader/main.py",
            ["yaml"],
            "Second Load (from state)",
        ),
        (
            "demos/05_lineage_resolver/main.py",
            [],
            "Field-Level Dependencies",
        ),
        (
            "demos/06_ml_pipeline/main.py",
            [],
            "Done!",
        ),
        (
            "demos/07_terraform_style_python_configs/main.py",
            [],
            "Second load (hits state + file cache)",
        ),
        (
            "demos/08_infra_as_code/main.py",
            [],
            "Second load (cache + state fast path)",
        ),
        (
            "demos/09_data_transformation/main.py",
            ["yaml", "jinja2"],
            "[13] Business Metrics Catalogue",
        ),
    ],
)
def test_demo_script_runs(
    script: str,
    required_modules: list[str],
    must_contain: str,
) -> None:
    missing = [name for name in required_modules if not _has_module(name)]
    if missing:
        pytest.skip(f"Missing optional dependency/dependencies: {missing}")

    script_path = REPO_ROOT / script
    assert script_path.exists(), f"Missing demo script: {script_path}"

    result = subprocess.run(
        [sys.executable, str(script_path)],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )

    if result.returncode != 0:
        stdout_tail = result.stdout[-4000:]
        stderr_tail = result.stderr[-4000:]
        raise AssertionError(
            f"Demo failed: {script}\n"
            f"returncode={result.returncode}\n"
            f"stdout tail:\n{stdout_tail}\n"
            f"stderr tail:\n{stderr_tail}"
        )

    assert must_contain in result.stdout
