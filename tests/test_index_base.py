"""Tests for Index ABC contract."""

from __future__ import annotations

import pytest

from assets.index.base import Index


class TestIndexABC:
    def test_cannot_instantiate_directly(self) -> None:
        with pytest.raises(TypeError, match="abstract"):
            Index()  # type: ignore[abstract]

    def test_subclass_must_implement_all_methods(self) -> None:
        class Incomplete(Index):
            pass

        with pytest.raises(TypeError, match="abstract"):
            Incomplete()  # type: ignore[abstract]

    def test_subclass_with_all_methods_can_instantiate(self) -> None:
        class Complete(Index):
            def put(self, location, *, group="", asset_id, fingerprint, deps=None):
                pass

            def remove(self, location, *, group=""):
                return False

            def stale_entries(self, changed_locations, *, group=""):
                return set()

        obj = Complete()
        assert obj.remove("x") is False
        assert obj.stale_entries(set()) == set()

    def test_context_manager_calls_close(self) -> None:
        closed = False

        class Closeable(Index):
            def put(self, location, *, group="", asset_id, fingerprint, deps=None):
                pass

            def remove(self, location, *, group=""):
                return False

            def stale_entries(self, changed_locations, *, group=""):
                return set()

            def close(self):
                nonlocal closed
                closed = True

        with Closeable():
            pass
        assert closed
