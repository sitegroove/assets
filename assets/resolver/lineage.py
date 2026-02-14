"""DependencyResolver — abstract base class for field-level dependency resolution.

The library provides this base class. Consumers implement concrete resolvers
(e.g., using sqlglot for column lineage, custom parsers for metrics, etc.)
by subclassing and implementing the ``resolve`` method.

Resolvers are registered on a :class:`~assets.core.registry.Registry` by name
and invoked via ``registry.resolve("kind", asset_id)``.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

from assets.core.dependency import FieldMapping

if TYPE_CHECKING:
    from assets.core.asset import Asset


class DependencyResolver(ABC):
    """Base class for field-level dependency resolvers.

    Consumers subclass this and implement ``resolve()`` with their own
    parsing logic.  The resolver receives the full :class:`Asset` object
    so it can read whichever fields it needs (``sql``, ``metadata``,
    ``children``, etc.).

    Example::

        class ColumnLineageResolver(DependencyResolver):
            def resolve(self, asset, schema):
                if not asset.sql:
                    return []
                # ... use sqlglot to trace columns ...

        class MetricsResolver(DependencyResolver):
            def resolve(self, asset, schema):
                # ... read asset.metadata["metrics"] ...
    """

    @abstractmethod
    def resolve(self, asset: Asset, schema: dict[str, list[str]]) -> list[FieldMapping]:
        """Resolve field-level dependencies for a single asset.

        Args:
            asset: The target asset to resolve dependencies for.
                Resolvers read whichever fields they need (``sql``,
                ``metadata``, ``children``, etc.).
            schema: ``{upstream_asset_id: [child_id, ...]}`` for each
                upstream asset referenced in ``asset.depends_on``.

        Returns:
            List of FieldMapping entries with path-based source/target
            (e.g., ``source="raw.users/email"``,
            ``target="staging.users/email_clean"``).
        """
        ...
