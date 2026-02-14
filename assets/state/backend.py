"""StateBackend — abstract interface for state persistence."""

from __future__ import annotations

from abc import ABC, abstractmethod
from contextlib import contextmanager
from typing import Any

from assets.state.models import StateSnapshot


class StateBackend(ABC):
    """Abstract base class for state storage backends.

    All backends support the context manager protocol for
    clean resource management::

        with SQLiteBackend() as backend:
            state = backend.load("production")
    """

    @abstractmethod
    def load(self, environment: str) -> StateSnapshot | None:
        """Load state for an environment. Returns None if not found."""
        ...

    @abstractmethod
    def save(
        self,
        environment: str,
        state: StateSnapshot,
        *,
        changed_ids: set[str] | None = None,
    ) -> None:
        """Save state for an environment.

        Args:
            environment: Target environment name.
            state: Full snapshot (assets, dependencies, metadata).
            changed_ids: When provided, only upsert these asset IDs
                and their dependencies.  ``None`` means upsert all
                (full save, backward compatible).
        """
        ...

    @abstractmethod
    @contextmanager
    def lock(self, environment: str):
        """Acquire a lock for concurrent access to an environment's state.

        Must be implemented as a context manager (use @contextmanager).
        """
        yield

    @abstractmethod
    def list_environments(self) -> list[str]:
        """List all environments with stored state."""
        ...

    @abstractmethod
    def delete_environment(self, environment: str) -> None:
        """Delete state for an environment."""
        ...

    def close(self) -> None:
        """Release any held resources (connections, file handles).

        Default implementation is a no-op. Override in backends
        that hold persistent resources (e.g., SQLite connections).
        """

    def __enter__(self) -> StateBackend:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()
