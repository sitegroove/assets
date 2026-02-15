"""Environment configuration for multi-environment state management."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class Environment(BaseModel):
    """A single environment definition."""

    name: str
    parent: str | None = None
    shallow: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)


class EnvironmentConfig(BaseModel):
    """Configuration for all environments."""

    environments: dict[str, Environment] = Field(default_factory=dict)
    default: str = "default"
    protected: set[str] = Field(default_factory=lambda: {"production"})
    allow_implicit_environments: bool = True

    def get(self, name: str | None = None) -> Environment:
        """Get environment by name, falling back to default.

        If the environment is not configured, creates one on the fly with
        default settings. This allows ad-hoc environment names (e.g., PR branches)
        without requiring pre-configuration.
        """
        env_name = name or self.default
        if env_name in self.environments:
            return self.environments[env_name]
        if self.allow_implicit_environments:
            # Auto-create for unknown names (e.g., ephemeral PR environments)
            return Environment(name=env_name)
        available = sorted(self.environments.keys())
        raise ValueError(
            f"Environment '{env_name}' is not configured. "
            f"Available: {available}. "
            "Set allow_implicit_environments=True to auto-create unknown names."
        )
