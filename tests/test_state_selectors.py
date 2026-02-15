"""Tests for state-aware selectors."""

from __future__ import annotations

from assets import (
    Asset,
    Environment,
    EnvironmentConfig,
    Registry,
    SQLiteBackend,
    StateManager,
    StateSelector,
)


def _build_manager() -> tuple[Registry, StateManager]:
    registry = Registry()
    manager = StateManager(
        registry,
        SQLiteBackend.memory(),
        EnvironmentConfig(
            default="production",
            environments={"production": Environment(name="production")},
        ),
    )
    return registry, manager


def _register_baseline(registry: Registry) -> None:
    registry.clear()
    registry.register_many(
        [
            Asset(id="raw.users", type="source", tags=["raw"]),
            Asset(
                id="staging.users",
                type="data_model",
                tags=["staging", "pii"],
                sql="SELECT * FROM raw.users",
                depends_on=["raw.users"],
            ),
            Asset(
                id="mart.users",
                type="data_model",
                tags=["mart"],
                sql="SELECT * FROM staging.users",
                depends_on=["staging.users"],
            ),
            Asset(id="temp.old", type="data_model", tags=["legacy"]),
        ]
    )


def _register_changed(registry: Registry) -> None:
    registry.clear()
    registry.register_many(
        [
            Asset(id="raw.users", type="source", tags=["raw"]),
            Asset(
                id="staging.users",
                type="data_model",
                tags=["staging", "pii"],
                sql="SELECT user_id FROM raw.users",
                depends_on=["raw.users"],
            ),
            Asset(
                id="mart.users",
                type="data_model",
                tags=["mart"],
                sql="SELECT * FROM staging.users",
                depends_on=["staging.users"],
            ),
            Asset(id="raw.events", type="source", tags=["raw"]),
        ]
    )


class TestStateSelectors:
    def test_empty_selector_returns_warning(self) -> None:
        registry, manager = _build_manager()
        selector = StateSelector(registry, manager)

        result = selector.execute("")
        assert result.names == set()
        assert any("empty" in warning.lower() for warning in result.warnings)

    def test_state_types(self) -> None:
        registry, manager = _build_manager()
        _register_baseline(registry)
        manager.apply(manager.plan(environment="production"), environment="production")

        _register_changed(registry)
        selector = StateSelector(registry, manager)

        modified = selector.execute("state:modified", environment="production")
        created = selector.execute("state:created", environment="production")
        updated = selector.execute("state:updated", environment="production")
        deleted = selector.execute("state:deleted", environment="production")

        assert modified.names == {"staging.users", "raw.events", "temp.old"}
        assert created.names == {"raw.events"}
        assert updated.names == {"staging.users"}
        assert deleted.names == {"temp.old"}

    def test_state_modified_with_graph_expansion(self) -> None:
        registry, manager = _build_manager()
        _register_baseline(registry)
        manager.apply(manager.plan(environment="production"), environment="production")

        _register_changed(registry)
        selector = StateSelector(registry, manager)

        downstream = selector.execute("state:modified+", environment="production")
        upstream = selector.execute("+state:modified", environment="production")

        assert downstream.names == {
            "staging.users",
            "mart.users",
            "raw.events",
            "temp.old",
        }
        assert upstream.names == {
            "raw.users",
            "staging.users",
            "raw.events",
            "temp.old",
        }

    def test_state_intersection_with_graph_selector(self) -> None:
        registry, manager = _build_manager()
        _register_baseline(registry)
        manager.apply(manager.plan(environment="production"), environment="production")

        _register_changed(registry)
        selector = StateSelector(registry, manager)

        result = selector.execute("state:modified,tag:pii", environment="production")
        assert result.names == {"staging.users"}

    def test_state_selector_without_state_manager(self) -> None:
        registry, _manager = _build_manager()
        _register_changed(registry)

        selector = StateSelector(registry)
        result = selector.execute("state:modified")

        assert result.names == set()
        assert any("requires a StateManager" in w for w in result.warnings)

    def test_state_selector_unknown_value_warns(self) -> None:
        registry, manager = _build_manager()
        _register_changed(registry)

        selector = StateSelector(registry, manager)
        result = selector.execute("state:unknown", environment="production")

        assert result.names == set()
        assert any("Unknown state selector" in w for w in result.warnings)

    def test_state_selector_empty_value_warns(self) -> None:
        registry, manager = _build_manager()
        _register_changed(registry)

        selector = StateSelector(registry, manager)
        result = selector.execute("state:", environment="production")

        assert result.names == set()
        assert any("empty value" in w for w in result.warnings)

    def test_state_selector_unexpected_plus_warns(self) -> None:
        registry, manager = _build_manager()
        _register_changed(registry)

        selector = StateSelector(registry, manager)
        result = selector.execute("state:modified+abc", environment="production")

        assert result.names == set()
        assert any("unexpected '+'" in w for w in result.warnings)

    def test_state_selector_modified_no_changes(self) -> None:
        registry, manager = _build_manager()
        _register_baseline(registry)
        manager.apply(manager.plan(environment="production"), environment="production")

        selector = StateSelector(registry, manager)
        result = selector.execute("state:modified", environment="production")

        assert result.names == set()

    def test_state_selector_trailing_comma_warns_empty_term(self) -> None:
        registry, manager = _build_manager()
        _register_baseline(registry)
        manager.apply(manager.plan(environment="production"), environment="production")
        _register_changed(registry)

        selector = StateSelector(registry, manager)
        result = selector.execute("state:modified,", environment="production")

        assert result.names == set()
        assert any("Selector is empty" in w for w in result.warnings)

    def test_state_selector_invalid_state_syntax_internal(self) -> None:
        registry, manager = _build_manager()
        selector = StateSelector(registry, manager)

        names, warnings = selector._resolve_state_term("state:modified\n+", plan=None)

        assert names == set()
        assert any("Invalid selector syntax" in w for w in warnings)

    def test_is_state_term_false_for_empty_string(self) -> None:
        registry, manager = _build_manager()
        selector = StateSelector(registry, manager)

        assert selector._is_state_term("") is False

    def test_state_selector_delegates_non_state_terms(self) -> None:
        registry, manager = _build_manager()
        _register_changed(registry)

        selector = StateSelector(registry, manager)
        result = selector.execute("tag:pii")

        assert result.names == {"staging.users"}
