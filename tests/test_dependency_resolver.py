"""Tests for DependencyResolver base class."""

import pytest

from assets import Asset, DependencyResolver, FieldMapping, Registry


class MockDependencyResolver(DependencyResolver):
    """Concrete implementation for testing."""

    def resolve(self, asset: Asset, schema: dict[str, list[str]]) -> list[FieldMapping]:
        return [
            FieldMapping(
                source="raw.users/email",
                target="staging.users/email_clean",
                transform="LOWER(TRIM(...))",
            )
        ]


class TestDependencyResolver:
    def test_abstract_cannot_instantiate(self):
        with pytest.raises(TypeError):
            DependencyResolver()  # type: ignore[abstract]

    def test_concrete_implementation(self):
        resolver = MockDependencyResolver()
        asset = Asset(id="staging.users", sql="SELECT ...")
        result = resolver.resolve(asset, {"raw.users": ["email"]})
        assert len(result) == 1
        assert result[0].source_field == "email"
        assert result[0].target_field == "email_clean"

    def test_resolve_via_registry(self):
        registry = Registry()
        upstream = Asset(id="raw.users", type="source")
        downstream = Asset(
            id="staging.users",
            sql="SELECT * FROM raw.users",
            depends_on=["raw.users"],
        )
        registry.register(upstream)
        registry.register(downstream)

        resolver = MockDependencyResolver()
        result = registry.resolve_field_dependency(
            asset_id="staging.users", resolver=resolver
        )
        assert len(result) == 1

    def test_resolve_via_named_registry_resolver(self):
        registry = Registry(resolvers={"lineage": MockDependencyResolver()})
        upstream = Asset(id="raw.users", type="source")
        downstream = Asset(
            id="staging.users",
            sql="SELECT * FROM raw.users",
            depends_on=["raw.users"],
        )
        registry.register(upstream)
        registry.register(downstream)

        result = registry.resolve("lineage", asset_id="staging.users")
        assert len(result) == 1
