"""Tests for AssetField metadata."""

from assets.core.fields import (
    FINGERPRINT_KEY,
    AssetField,
)


class TestAssetField:
    def test_default_fingerprint_true(self):
        f = AssetField()
        extra = f.json_schema_extra
        assert extra[FINGERPRINT_KEY] is True

    def test_fingerprint_false(self):
        f = AssetField(fingerprint=False)
        extra = f.json_schema_extra
        assert extra[FINGERPRINT_KEY] is False

    def test_default_value(self):
        f = AssetField(default=42)
        assert f.default == 42

    def test_passes_kwargs_through(self):
        f = AssetField(default="x", description="test desc")
        assert f.description == "test desc"
