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
    (which may be a consumer subclass) so it can read whichever
    consumer-defined fields it needs.

    Example::

        class ColumnLineageResolver(DependencyResolver):
            def resolve(self, asset, schema):
                sql = getattr(asset, "sql", None)
                if not sql:
                    return []
                # ... use sqlglot to trace columns ...

        class MetricsResolver(DependencyResolver):
            def resolve(self, asset, schema):
                # ... read consumer-defined fields ...
    """

    @abstractmethod
    def resolve(self, asset: Asset, schema: dict[str, list[str]]) -> list[FieldMapping]:
        """Resolve field-level dependencies for a single asset.

        Args:
            asset: The target asset to resolve dependencies for.
                Resolvers read whichever consumer-defined fields they
                need (the base Asset carries only identity, graph,
                and classification fields).
            schema: ``{upstream_asset_id: [field/child_id, ...]}``
                for each upstream asset in ``asset.depends_on``.
                Values are namespaced paths (``"field_name/child_id"``).

        Returns:
            List of FieldMapping entries with path-based source/target
            (e.g., ``source="raw.users/columns/email"``,
            ``target="staging.users/columns/email_clean"``).
        """
        ...
