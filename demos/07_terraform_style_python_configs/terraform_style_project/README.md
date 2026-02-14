# Terraform-Style Python Config Project

This demo models a realistic configuration-only project:

- `project_models.py` defines shared Pydantic asset models.
- `resources/*.py` declare assets as plain data objects (`ASSET` or `ASSETS`).
- No config module performs state I/O, planning, or apply logic.

Run the loader and planner with:

```bash
python demos/07_terraform_style_python_configs/main.py
```
