"""Tests for the selector parser."""

from assets import Asset, AssetField, Registry


class TestSelectors:
    def test_exact_match(self, populated_registry: Registry):
        result = populated_registry.select("raw.users")
        assert result.names == {"raw.users"}
        assert len(result.assets) == 1

    def test_tag_selector(self, populated_registry: Registry):
        result = populated_registry.select("tag:raw")
        assert result.names == {"raw.users", "raw.payments"}

    def test_kind_selector(self, populated_registry: Registry):
        result = populated_registry.select("kind:source")
        assert result.names == {"raw.users", "raw.payments"}

    def test_kind_data_model(self, populated_registry: Registry):
        result = populated_registry.select("kind:data_model")
        assert "staging.users" in result.names
        assert "mart.enriched" in result.names

    def test_wildcard(self, populated_registry: Registry):
        result = populated_registry.select("raw.*")
        assert result.names == {"raw.users", "raw.payments"}

    def test_upstream_ancestor(self, populated_registry: Registry):
        result = populated_registry.select("+mart.enriched")
        assert "mart.enriched" in result.names
        assert "staging.users" in result.names
        assert "staging.payments" in result.names
        assert "raw.users" in result.names

    def test_downstream_descendant(self, populated_registry: Registry):
        result = populated_registry.select("raw.users+")
        assert "raw.users" in result.names
        assert "staging.users" in result.names
        assert "mart.enriched" in result.names

    def test_both_directions(self, populated_registry: Registry):
        result = populated_registry.select("+staging.users+")
        assert "raw.users" in result.names
        assert "staging.users" in result.names
        assert "mart.enriched" in result.names

    def test_depth_limited(self, populated_registry: Registry):
        result = populated_registry.select("raw.users+1")
        assert "raw.users" in result.names
        assert "staging.users" in result.names
        assert "mart.enriched" not in result.names

    def test_intersection(self, populated_registry: Registry):
        result = populated_registry.select("tag:staging,kind:data_model")
        assert "staging.users" in result.names
        assert "staging.payments" in result.names
        # mart.enriched is kind:data_model but not tag:staging
        assert "mart.enriched" not in result.names

    def test_no_match(self, populated_registry: Registry):
        result = populated_registry.select("nonexistent")
        assert result.names == set()
        assert result.assets == []

    def test_tag_no_match(self, populated_registry: Registry):
        result = populated_registry.select("tag:nonexistent")
        assert result.names == set()


class Column(Asset):
    type: str = ""


class DataModel(Asset):
    columns: list[Column] = AssetField(default_factory=list, field_source=True)


class TestNestedSelectors:
    def test_children_selector(self):
        registry = Registry()
        m = DataModel(
            name="staging.users",
            columns=[Column(name="id"), Column(name="email")],
        )
        registry.register(m)
        result = registry.select("children:staging.users")
        assert result.names == {"staging.users/id", "staging.users/email"}

    def test_children_selector_no_children(self):
        registry = Registry()
        registry.register(Asset(name="raw.users"))
        result = registry.select("children:raw.users")
        assert result.names == set()

    def test_parent_selector(self):
        registry = Registry()
        m = DataModel(
            name="staging.users",
            columns=[Column(name="email")],
        )
        registry.register(m)
        result = registry.select("parent:staging.users/email")
        assert result.names == {"staging.users"}

    def test_parent_selector_top_level(self):
        registry = Registry()
        registry.register(Asset(name="raw.users"))
        result = registry.select("parent:raw.users")
        assert result.names == set()

    def test_top_selector_all(self):
        registry = Registry()
        m = DataModel(
            name="staging.users",
            columns=[Column(name="email")],
        )
        registry.register(m)
        registry.register(Asset(name="raw.users"))
        result = registry.select("top:*")
        assert result.names == {"staging.users", "raw.users"}
        assert "staging.users/email" not in result.names

    def test_top_selector_pattern(self):
        registry = Registry()
        registry.register(Asset(name="raw.users"))
        registry.register(Asset(name="staging.users"))
        result = registry.select("top:raw.*")
        assert result.names == {"raw.users"}
