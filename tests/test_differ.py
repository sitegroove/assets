"""Tests for the Differ."""

from __future__ import annotations

from typing import cast

from assets import Asset, AssetField, Differ
from assets.state.models import AssetState


class Column(Asset):
    type: str = ""


class HasColumns(Asset):
    """Asset subclass with a children field for differ tests."""

    columns: list[Column] = cast(
        list[Column],
        AssetField(default_factory=list, children=True),
    )


class TestDiffer:
    def setup_method(self):
        self.differ = Differ()

    def test_no_changes(self):
        asset = Asset(id="test", type="model")
        current = {
            "test": AssetState(
                id="test",
                fingerprint=asset.fingerprint,
                data=asset.model_dump(),
            )
        }
        cs = self.differ.diff([asset], current)
        assert len(cs.asset_changes) == 0

    def test_create(self):
        asset = Asset(id="new", type="model")
        cs = self.differ.diff([asset], {})
        assert len(cs.asset_changes) == 1
        change = cs.asset_changes[0]
        assert change.action == "create"
        assert change.asset_id == "new"
        assert change.after is not None

    def test_delete(self):
        current = {
            "old": AssetState(
                id="old",
                fingerprint="abc",
                data={"id": "old"},
            )
        }
        cs = self.differ.diff([], current)
        assert len(cs.asset_changes) == 1
        change = cs.asset_changes[0]
        assert change.action == "delete"
        assert change.asset_id == "old"

    def test_update(self):
        asset = Asset(id="test", type="model_v2")
        current = {
            "test": AssetState(
                id="test",
                fingerprint="old_fingerprint",
                data={"id": "test", "type": "model_v1"},
            )
        }
        cs = self.differ.diff([asset], current)
        assert len(cs.asset_changes) == 1
        change = cs.asset_changes[0]
        assert change.action == "update"
        assert any(fc.field == "type" for fc in change.field_changes)

    def test_existing_asset_with_different_fingerprint_is_update(self):
        """Without tombstones, an existing asset with different fingerprint is an update."""
        asset = Asset(id="test")
        current = {"test": AssetState(id="test", fingerprint="x")}
        cs = self.differ.diff([asset], current)
        assert cs.asset_changes[0].action == "update"

    def test_field_changes_detail(self):
        asset = Asset(id="test", description="new desc", tags=["a", "b"])
        current = {
            "test": AssetState(
                id="test",
                fingerprint="old",
                data={"id": "test", "description": "old desc", "tags": ["a"]},
            )
        }
        cs = self.differ.diff([asset], current)
        change = cs.asset_changes[0]
        fields_changed = {fc.field for fc in change.field_changes}
        assert "description" in fields_changed
        assert "tags" in fields_changed

    def test_mixed_operations(self):
        desired = [
            Asset(id="keep", type="model"),
            Asset(id="new", type="source"),
            Asset(id="changed", type="v2"),
        ]
        keep = Asset(id="keep", type="model")
        current = {
            "keep": AssetState(
                id="keep", fingerprint=keep.fingerprint, data=keep.model_dump()
            ),
            "changed": AssetState(
                id="changed",
                fingerprint="old",
                data={"id": "changed", "type": "v1"},
            ),
            "deleted": AssetState(
                id="deleted", fingerprint="x", data={"id": "deleted"}
            ),
        }
        cs = self.differ.diff(desired, current)
        actions = {c.asset_id: c.action for c in cs.asset_changes}
        assert "keep" not in actions  # unchanged
        assert actions["new"] == "create"
        assert actions["changed"] == "update"
        assert actions["deleted"] == "delete"

    def test_nested_children_change_detected(self):
        asset_v1 = HasColumns(
            id="test",
            columns=[Column(id="col1", type="column")],
        )
        asset_v2 = HasColumns(
            id="test",
            columns=[
                Column(id="col1", type="column"),
                Column(id="col2", type="column"),
            ],
        )
        current = {
            "test": AssetState(
                id="test",
                fingerprint=asset_v1.fingerprint,
                data=asset_v1.model_dump(),
            )
        }
        cs = self.differ.diff([asset_v2], current)
        assert len(cs.asset_changes) == 1
        assert cs.asset_changes[0].action == "update"
        fields_changed = {fc.field for fc in cs.asset_changes[0].field_changes}
        assert "columns" in fields_changed

    def test_deeply_nested_change_detected(self):
        """Changing a child's field triggers an update on the parent."""

        class SubColumn(Asset):
            sub_items: list[Column] = cast(
                list[Column],
                AssetField(default_factory=list, children=True),
            )

        class HasSub(Asset):
            columns: list[SubColumn] = cast(
                list[SubColumn],
                AssetField(default_factory=list, children=True),
            )

        child_v1 = Column(id="sub", type="v1")
        child_v2 = Column(id="sub", type="v2")
        asset_v1 = HasSub(
            id="test",
            columns=[SubColumn(id="parent", sub_items=[child_v1])],
        )
        asset_v2 = HasSub(
            id="test",
            columns=[SubColumn(id="parent", sub_items=[child_v2])],
        )
        current = {
            "test": AssetState(
                id="test",
                fingerprint=asset_v1.fingerprint,
                data=asset_v1.model_dump(),
            )
        }
        cs = self.differ.diff([asset_v2], current)
        assert len(cs.asset_changes) == 1
        assert cs.asset_changes[0].action == "update"
