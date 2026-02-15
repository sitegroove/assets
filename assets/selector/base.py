"""Selector base class."""

from __future__ import annotations

from abc import ABC, abstractmethod

from assets.core.graph import SelectionResult


class Selector(ABC):
    """Abstract selector interface."""

    @abstractmethod
    def execute(self, selector: str) -> SelectionResult:
        """Execute a selector expression and return matching assets."""
