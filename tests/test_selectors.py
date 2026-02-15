"""Tests for graph selectors."""

from assets import Asset, GraphSelector, Registry


class TestSelectors:
    def test_exact_match(self, populated_registry: Registry):
        result = GraphSelector(populated_registry).execute("raw.users")
        assert result.names == {"raw.users"}
        assert len(result.assets) == 1

    def test_tag_selector(self, populated_registry: Registry):
        result = GraphSelector(populated_registry).execute("tag:raw")
        assert result.names == {"raw.users", "raw.payments"}

    def test_kind_selector(self, populated_registry: Registry):
        result = GraphSelector(populated_registry).execute("type:source")
        assert result.names == {"raw.users", "raw.payments"}

    def test_kind_data_model(self, populated_registry: Registry):
        result = GraphSelector(populated_registry).execute("type:data_model")
        assert "staging.users" in result.names
        assert "mart.enriched" in result.names

    def test_wildcard(self, populated_registry: Registry):
        result = GraphSelector(populated_registry).execute("raw.*")
        assert result.names == {"raw.users", "raw.payments"}

    def test_upstream_ancestor(self, populated_registry: Registry):
        result = GraphSelector(populated_registry).execute("+mart.enriched")
        assert "mart.enriched" in result.names
        assert "staging.users" in result.names
        assert "staging.payments" in result.names
        assert "raw.users" in result.names

    def test_downstream_descendant(self, populated_registry: Registry):
        result = GraphSelector(populated_registry).execute("raw.users+")
        assert "raw.users" in result.names
        assert "staging.users" in result.names
        assert "mart.enriched" in result.names

    def test_both_directions(self, populated_registry: Registry):
        result = GraphSelector(populated_registry).execute("+staging.users+")
        assert "raw.users" in result.names
        assert "staging.users" in result.names
        assert "mart.enriched" in result.names

    def test_depth_limited(self, populated_registry: Registry):
        result = GraphSelector(populated_registry).execute("raw.users+1")
        assert "raw.users" in result.names
        assert "staging.users" in result.names
        assert "mart.enriched" not in result.names

    def test_intersection(self, populated_registry: Registry):
        result = GraphSelector(populated_registry).execute(
            "tag:staging,type:data_model"
        )
        assert "staging.users" in result.names
        assert "staging.payments" in result.names
        # mart.enriched is kind:data_model but not tag:staging
        assert "mart.enriched" not in result.names

    def test_no_match(self, populated_registry: Registry):
        result = GraphSelector(populated_registry).execute("nonexistent")
        assert result.names == set()
        assert result.assets == []

    def test_tag_no_match(self, populated_registry: Registry):
        result = GraphSelector(populated_registry).execute("tag:nonexistent")
        assert result.names == set()

    def test_empty_string_returns_empty(self, populated_registry: Registry):
        result = GraphSelector(populated_registry).execute("")
        assert result.names == set()
        assert result.assets == []
        assert "empty" in result.warnings[0].lower()

    def test_whitespace_returns_empty(self, populated_registry: Registry):
        result = GraphSelector(populated_registry).execute("   ")
        assert result.names == set()
        assert "empty" in result.warnings[0].lower()

    def test_nonexistent_asset_in_graph_traversal(self, populated_registry: Registry):
        result = GraphSelector(populated_registry).execute("+nonexistent+")
        assert result.names == set()

    def test_wildcard_no_match(self, populated_registry: Registry):
        result = GraphSelector(populated_registry).execute("nonexistent.*")
        assert result.names == set()

    def test_non_numeric_depth_absorbed_into_name(self, populated_registry: Registry):
        # "raw.users+abc" — regex absorbs "+abc" into the name, no crash
        result = GraphSelector(populated_registry).execute("raw.users+abc")
        assert result.names == set()
        assert any("unexpected '+'" in warning for warning in result.warnings)

    def test_upstream_depth_limited(self, populated_registry: Registry):
        result = GraphSelector(populated_registry).execute("+1mart.enriched")
        # Leading "+1" is upstream, but regex captures "+" as upstream,
        # "1mart.enriched" as name.
        # No asset named "1mart.enriched" exists, so empty
        assert result.names == set()
        assert any("+<digit>" in warning for warning in result.warnings)

    def test_kind_empty_string(self, populated_registry: Registry):
        # Assets with type="" should match type:
        # but "type:" with empty value selects type == "".
        result = GraphSelector(populated_registry).execute("type:")
        # No assets should have empty kind (all have explicit kinds in fixtures)
        # This verifies the edge case doesn't crash
        assert isinstance(result.names, set)
        assert any("empty value" in warning for warning in result.warnings)

    def test_tag_empty_string(self, populated_registry: Registry):
        result = GraphSelector(populated_registry).execute("tag:")
        assert isinstance(result.names, set)
        assert any("empty value" in warning for warning in result.warnings)

    def test_intersection_with_empty_term(self, populated_registry: Registry):
        result = GraphSelector(populated_registry).execute("tag:raw,")
        assert result.names == set()

    def test_invalid_selector_syntax(self, populated_registry: Registry):
        result = GraphSelector(populated_registry).execute("raw.users\n+")
        assert result.names == set()
        assert any("Invalid selector syntax" in warning for warning in result.warnings)

    def test_warning_with_existing_plus_digit_name(self, registry: Registry):
        registry.register(Asset(id="1asset", type="source"))
        result = GraphSelector(registry).execute("+1asset")
        assert result.names == {"1asset"}
        assert any("+<digit>" in warning for warning in result.warnings)
