"""Tests for the Differ."""

from assets import Asset, Differ
from assets.state.models import AssetState


class TestDiffer:
    def setup_method(self):
        self.differ = Differ()

    def test_no_changes(self):
        asset = Asset(name="test", kind="model")
        current = {
            "test": AssetState(
                name="test",
                fingerprint=asset.fingerprint,
                data=asset.model_dump(),
            )
        }
        cs = self.differ.diff([asset], current)
        assert len(cs.asset_changes) == 0

    def test_create(self):
        asset = Asset(name="new", kind="model")
        cs = self.differ.diff([asset], {})
        assert len(cs.asset_changes) == 1
        change = cs.asset_changes[0]
        assert change.action == "create"
        assert change.asset_name == "new"
        assert change.after is not None

    def test_delete(self):
        current = {
            "old": AssetState(
                name="old",
                fingerprint="abc",
                data={"name": "old"},
            )
        }
        cs = self.differ.diff([], current)
        assert len(cs.asset_changes) == 1
        change = cs.asset_changes[0]
        assert change.action == "delete"
        assert change.asset_name == "old"

    def test_update(self):
        asset = Asset(name="test", kind="model_v2")
        current = {
            "test": AssetState(
                name="test",
                fingerprint="old_fingerprint",
                data={"name": "test", "kind": "model_v1"},
            )
        }
        cs = self.differ.diff([asset], current)
        assert len(cs.asset_changes) == 1
        change = cs.asset_changes[0]
        assert change.action == "update"
        assert any(fc.field == "kind" for fc in change.field_changes)

    def test_skip_deleted_tombstones(self):
        asset = Asset(name="test")
        current = {
            "test": AssetState(name="test", fingerprint="x", deleted=True)
        }
        cs = self.differ.diff([asset], current)
        assert cs.asset_changes[0].action == "create"

    def test_field_changes_detail(self):
        asset = Asset(name="test", description="new desc", tags=["a", "b"])
        current = {
            "test": AssetState(
                name="test",
                fingerprint="old",
                data={"name": "test", "description": "old desc", "tags": ["a"]},
            )
        }
        cs = self.differ.diff([asset], current)
        change = cs.asset_changes[0]
        fields_changed = {fc.field for fc in change.field_changes}
        assert "description" in fields_changed
        assert "tags" in fields_changed

    def test_mixed_operations(self):
        desired = [
            Asset(name="keep", kind="model"),
            Asset(name="new", kind="source"),
            Asset(name="changed", kind="v2"),
        ]
        keep = Asset(name="keep", kind="model")
        current = {
            "keep": AssetState(
                name="keep", fingerprint=keep.fingerprint, data=keep.model_dump()
            ),
            "changed": AssetState(
                name="changed", fingerprint="old", data={"name": "changed", "kind": "v1"}
            ),
            "deleted": AssetState(
                name="deleted", fingerprint="x", data={"name": "deleted"}
            ),
        }
        cs = self.differ.diff(desired, current)
        actions = {c.asset_name: c.action for c in cs.asset_changes}
        assert "keep" not in actions  # unchanged
        assert actions["new"] == "create"
        assert actions["changed"] == "update"
        assert actions["deleted"] == "delete"
