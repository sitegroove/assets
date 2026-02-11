"""MemoryBackend — in-memory state backend for testing."""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager

from assets.state.backend import StateBackend
from assets.state.models import StateSnapshot


class MemoryBackend(StateBackend):
    """In-memory state backend. No file I/O. Useful for testing."""

    def __init__(self) -> None:
        self._states: dict[str, StateSnapshot] = {}
        self._locks: set[str] = set()

    def load(self, environment: str) -> StateSnapshot | None:
        return self._states.get(environment)

    def save(self, environment: str, state: StateSnapshot) -> None:
        self._states[environment] = state

    @contextmanager
    def lock(self, environment: str) -> Generator[None, None, None]:
        if environment in self._locks:
            raise RuntimeError(f"Environment '{environment}' is already locked")
        self._locks.add(environment)
        try:
            yield
        finally:
            self._locks.discard(environment)

    def list_environments(self) -> list[str]:
        return list(self._states.keys())

    def delete_environment(self, environment: str) -> None:
        self._states.pop(environment, None)
