"""Index — abstract base for entry indexes with dependency tracking."""

from __future__ import annotations

from abc import ABC, abstractmethod


class Index(ABC):
    """Abstract base for freshness-tracking indexes with dependency edges.

    An index tracks entries keyed by a ``(group, location)`` pair,
    along with dependency edges to detect when an entry needs
    re-processing.  The index does **not** store compiled data — that
    lives in the state backend's ``assets`` table after ``apply()``.

    The *group* allows multiple independent namespaces within the
    same index (e.g. modules installed in different locations).
    Entries in different groups may share the same *location* without
    collision.

    Subclasses implement storage, change detection, and staleness
    logic for their specific domain (files, database tables, APIs).
    """

    @abstractmethod
    def put(
        self,
        location: str,
        *,
        group: str = "",
        asset_id: str,
        fingerprint: str,
        deps: list[tuple[str, str]] | None = None,
    ) -> None:
        """Store an entry with optional dependencies.

        Args:
            location: Identifier for this entry within its *group*.
            group: Logical namespace for this entry.
            asset_id: The asset id this entry produces.
            fingerprint: Content fingerprint of the compiled entry.
            deps: List of ``(dep_location, dep_kind)`` tuples this
                entry depends on.  Interpretation of *location* and
                *kind* is subclass-specific.
        """
        ...

    @abstractmethod
    def remove(self, location: str, *, group: str = "") -> bool:
        """Remove an entry and its dependencies. Returns True if found."""
        ...

    @abstractmethod
    def stale_entries(
        self,
        changed_locations: set[str],
        *,
        group: str = "",
    ) -> set[str]:
        """Return entry locations that depend on any of the changed locations.

        This is the reverse lookup: given that these dependencies changed,
        which entries are affected?
        """
        ...

    def close(self) -> None:
        """Release resources. Default no-op."""

    def __enter__(self) -> Index:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
