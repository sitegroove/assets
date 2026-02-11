"""Environment configuration for multi-environment state management."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel


class Environment(BaseModel):
    """A single environment definition."""

    name: str
    parent: str | None = None
    shallow: bool = False
    metadata: dict[str, Any] = {}


class EnvironmentConfig(BaseModel):
    """Configuration for all environments."""

    environments: dict[str, Environment] = {}
    default: str = "development"

    def get(self, name: str | None = None) -> Environment:
        """Get environment by name, falling back to default."""
        env_name = name or self.default
        if env_name in self.environments:
            return self.environments[env_name]
        return Environment(name=env_name)
