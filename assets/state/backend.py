"""StateBackend — abstract interface for state persistence."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Generator
from contextlib import contextmanager

from assets.state.models import StateSnapshot


class StateBackend(ABC):
    """Abstract base class for state storage backends."""

    @abstractmethod
    def load(self, environment: str) -> StateSnapshot | None:
        """Load state for an environment. Returns None if not found."""
        ...

    @abstractmethod
    def save(self, environment: str, state: StateSnapshot) -> None:
        """Save state for an environment."""
        ...

    @abstractmethod
    @contextmanager
    def lock(self, environment: str) -> Generator[None, None, None]:
        """Acquire a lock for concurrent access to an environment's state."""
        ...

    @abstractmethod
    def list_environments(self) -> list[str]:
        """List all environments with stored state."""
        ...

    @abstractmethod
    def delete_environment(self, environment: str) -> None:
        """Delete state for an environment."""
        ...
