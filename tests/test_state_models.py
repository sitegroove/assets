"""Tests for state models."""

from assets import AssetState, DependencyState, StateSnapshot


class TestAssetState:
    def test_basic(self):
        s = AssetState(id="test", fingerprint="abc")
        assert s.id == "test"
        assert s.version == 1
        assert s.deleted is False

    def test_with_data(self):
        s = AssetState(
            id="test",
            type="model",
            fingerprint="abc",
            data={"name": "test", "kind": "model"},
        )
        assert s.data["kind"] == "model"

    def test_deleted_tombstone(self):
        s = AssetState(id="test", fingerprint="abc", deleted=True)
        assert s.deleted is True


class TestDependencyState:
    def test_basic(self):
        d = DependencyState(source="a", target="b", fingerprint="abc")
        assert d.source == "a"
        assert d.type == ""


class TestStateSnapshot:
    def test_empty(self):
        s = StateSnapshot()
        assert s.version == 1
        assert s.assets == {}
        assert s.dependencies == []

    def test_with_assets(self):
        s = StateSnapshot(
            environment="production",
            assets={
                "test": AssetState(id="test", fingerprint="abc"),
            },
        )
        assert "test" in s.assets
        assert s.environment == "production"

    def test_serialization_roundtrip(self):
        s = StateSnapshot(
            environment="dev",
            assets={
                "a": AssetState(id="a", fingerprint="fp1", data={"name": "a"}),
            },
        )
        json_str = s.model_dump_json()
        s2 = StateSnapshot.model_validate_json(json_str)
        assert s2.environment == "dev"
        assert "a" in s2.assets
        assert s2.assets["a"].fingerprint == "fp1"
