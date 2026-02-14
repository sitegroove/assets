# AGENTS.md - assets Development Guide

This document provides guidelines for agentic coding assistants working on the graph-based asset registry.

# IMPORTANT!!!!
Library is still in alpha and any breaking changes are allowed.

## Project Overview

assets library is powered by Pydantic, sitting at the intersection of data cataloging (like DataHub/Amundsen) and infrastructure-as-code (like Terraform/Pulumi)

## Code Style Guidelines

### Imports
- Group imports: stdlib, third-party, local
- Sort imports within groups alphabetically
- Use absolute imports over relative imports
- Explicit re-exports in `__init__.py` using `__all__`

```python
# Good
import os
from typing import Any, Dict

from pydantic import BaseModel

from assets.types import MetricType

__all__ = ["AssetField", "Model", "Metric"]
```

### Formatting
- Line length: 88 characters (configured in pyproject.toml)
- Target Python version: 3.10+
- Use ruff for both linting and formatting
- Trailing commas in multi-line structures

### Types
- Use type hints everywhere (enforced by mypy)
- Prefer `from __future__ import annotations` for forward references
- Use `|` syntax for unions (Python 3.10+)
- Use `list[T]` and `dict[K, V]` instead of `List`, `Dict`
- Define explicit return types on all functions

```python
from __future__ import annotations

def process_metrics(metrics: list[Metric]) -> dict[str, Any]:
    ...
```

### Naming Conventions
- Classes: PascalCase (e.g., `FieldChange`, `Change`)
- Functions/variables: snake_case (e.g., `put_many`, `current_hash`)
- Constants: SCREAMING_SNAKE_CASE (e.g., `DEFAULT_TIMEOUT`)
- Private members: leading underscore (e.g., `_rel_path`)
- Abstract base classes: prefix with Abstract (e.g., `AbstractClass`)

### Error Handling
- Use specific exception types over generic `Exception`
- Define custom exceptions in `assets/exceptions.py`
- Always include context in error messages
- Use `raise from` when re-raising exceptions

### Documentation
- Use Google-style docstrings
- Include type information in docstrings for complex params
- Document all public APIs
- Keep docstrings under 88 chars per line

## Example Project Structure

```
├── assets/                # Main library code
├── tests/                 # Test suite
├── demos/                 # Demos of the consumer usage
├── readme.md              # Documentation
└── pyproject.toml         # Project configuration
```

## Key Dependencies

- **pydantic**: Data validation and asset configuration
- **fsspec**: Filesystem interfaces for Python
- **sqlite**: sqlite database

## Testing Philosophy

- Unit tests in `tests/`
- Use descriptive test function names
- Test both success and error cases
- Keep tests fast and isolated

## Documentation

- Write documentation and usage examples into readme.md
