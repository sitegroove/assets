"""LineageResolver — abstract base class for column-level lineage resolution.

The library provides this base class. Consumers implement concrete resolvers
(e.g., using sqlglot) by subclassing and implementing the `resolve` method.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from assets.core.dependency import FieldMapping


class LineageResolver(ABC):
    """Base class for column-level lineage resolvers.

    Consumers subclass this and implement `resolve()` with their own
    SQL parsing logic (e.g., sqlglot, custom parser, etc.).
    """

    @abstractmethod
    def resolve(self, sql: str, schema: dict[str, list[str]]) -> list[FieldMapping]:
        """Parse SQL and trace column lineage.

        Args:
            sql: Clean SQL (refs already resolved to table names).
            schema: {asset_name: [child_name, ...]} for upstream assets.

        Returns:
            List of FieldMapping entries with path-based source/target
            (e.g., source="raw.users/email", target="staging.users/email_clean").
        """
        ...
