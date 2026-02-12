"""Tests for Dependency and FieldMapping models."""

from assets import Dependency, FieldMapping


class TestDependency:
    def test_basic_creation(self):
        d = Dependency(source="raw.users", target="staging.users")
        assert d.source == "raw.users"
        assert d.target == "staging.users"
        assert d.type == "ref"

    def test_fingerprint_deterministic(self):
        d1 = Dependency(source="a", target="b", type="ref")
        d2 = Dependency(source="a", target="b", type="ref")
        assert d1.fingerprint == d2.fingerprint

    def test_fingerprint_changes(self):
        d1 = Dependency(source="a", target="b", type="ref")
        d2 = Dependency(source="a", target="c", type="ref")
        assert d1.fingerprint != d2.fingerprint

    def test_fingerprint_type_matters(self):
        d1 = Dependency(source="a", target="b", type="ref")
        d2 = Dependency(source="a", target="b", type="source")
        assert d1.fingerprint != d2.fingerprint

    def test_metadata(self):
        d = Dependency(source="a", target="b", metadata={"priority": "high"})
        assert d.metadata["priority"] == "high"


class TestFieldMapping:
    def test_path_based_creation(self):
        fm = FieldMapping(
            source="raw.users/email",
            target="staging.users/email_clean",
            transform="LOWER(TRIM(...))",
        )
        assert fm.source == "raw.users/email"
        assert fm.target == "staging.users/email_clean"
        assert fm.source_asset == "raw.users"
        assert fm.source_field == "email"
        assert fm.target_asset == "staging.users"
        assert fm.target_field == "email_clean"
        assert fm.transform == "LOWER(TRIM(...))"

    def test_no_transform(self):
        fm = FieldMapping(
            source="a/x",
            target="b/y",
        )
        assert fm.transform is None

    def test_legacy_constructor_backward_compat(self):
        fm = FieldMapping(
            source_asset="raw.users",
            source_field="email",
            target_asset="staging.users",
            target_field="email_clean",
        )
        assert fm.source == "raw.users/email"
        assert fm.target == "staging.users/email_clean"
        assert fm.source_asset == "raw.users"
        assert fm.source_field == "email"

    def test_asset_only_path(self):
        fm = FieldMapping(source="raw.users", target="staging.users")
        assert fm.source_asset == "raw.users"
        assert fm.source_field == ""
        assert fm.target_asset == "staging.users"
        assert fm.target_field == ""

    def test_deep_child_path(self):
        fm = FieldMapping(
            source="db/public/users/email",
            target="warehouse/analytics/user_emails/email",
        )
        assert fm.source_asset == "db"
        assert fm.source_field == "public/users/email"
        assert fm.target_asset == "warehouse"
        assert fm.target_field == "analytics/user_emails/email"
