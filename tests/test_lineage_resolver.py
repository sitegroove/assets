"""Tests for LineageResolver base class."""

import pytest

from assets import FieldMapping, LineageResolver


class MockLineageResolver(LineageResolver):
    """Concrete implementation for testing."""

    def resolve(self, sql: str, schema: dict[str, list[str]]) -> list[FieldMapping]:
        return [
            FieldMapping(
                source_asset="raw.users",
                source_field="email",
                target_asset="staging.users",
                target_field="email_clean",
                transform="LOWER(TRIM(...))",
            )
        ]


class TestLineageResolver:
    def test_abstract_cannot_instantiate(self):
        with pytest.raises(TypeError):
            LineageResolver()  # type: ignore[abstract]

    def test_concrete_implementation(self):
        resolver = MockLineageResolver()
        result = resolver.resolve("SELECT ...", {"raw.users": ["email"]})
        assert len(result) == 1
        assert result[0].source_field == "email"
        assert result[0].target_field == "email_clean"

    def test_resolve_via_registry(self):
        from assets import Asset, Registry

        registry = Registry()
        upstream = Asset(name="raw.users", kind="source")
        downstream = Asset(
            name="staging.users",
            sql="SELECT * FROM {{ ref('raw.users') }}",
        )
        registry.register(upstream)
        registry.register(downstream)

        resolver = MockLineageResolver()
        result = registry.resolve_field_dependency(
            asset_name="staging.users", resolver=resolver
        )
        assert len(result) == 1
