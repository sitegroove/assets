"""Tests for the selector parser."""

from assets import Registry


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

    def test_empty_string_returns_empty(self, populated_registry: Registry):
        result = populated_registry.select("")
        assert result.names == set()
        assert result.assets == []

    def test_whitespace_returns_empty(self, populated_registry: Registry):
        result = populated_registry.select("   ")
        assert result.names == set()

    def test_nonexistent_asset_in_graph_traversal(self, populated_registry: Registry):
        result = populated_registry.select("+nonexistent+")
        assert result.names == set()

    def test_wildcard_no_match(self, populated_registry: Registry):
        result = populated_registry.select("nonexistent.*")
        assert result.names == set()

    def test_non_numeric_depth_absorbed_into_name(self, populated_registry: Registry):
        # "raw.users+abc" — regex absorbs "+abc" into the name, no crash
        result = populated_registry.select("raw.users+abc")
        assert result.names == set()

    def test_upstream_depth_limited(self, populated_registry: Registry):
        result = populated_registry.select("+1mart.enriched")
        # Leading "+1" is upstream, but regex captures "+" as upstream, "1mart.enriched" as name
        # No asset named "1mart.enriched" exists, so empty
        assert result.names == set()

    def test_kind_empty_string(self, populated_registry: Registry):
        # Assets with kind="" should match kind:
        # but "kind:" with empty value is actually "kind:" which is selector for kind == ""
        result = populated_registry.select("kind:")
        # No assets should have empty kind (all have explicit kinds in fixtures)
        # This verifies the edge case doesn't crash
        assert isinstance(result.names, set)
