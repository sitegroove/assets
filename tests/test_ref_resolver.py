"""Tests for RefResolver."""

from assets import RefResolver


class TestRefResolver:
    def setup_method(self):
        self.resolver = RefResolver()

    def test_extract_single_ref(self):
        sql = "SELECT * FROM {{ ref('raw.users') }}"
        assert self.resolver.extract_refs(sql) == ["raw.users"]

    def test_extract_multiple_refs(self):
        sql = (
            "SELECT u.*, p.amount "
            "FROM {{ ref('raw.users') }} u "
            "JOIN {{ ref('raw.payments') }} p ON u.id = p.user_id"
        )
        assert self.resolver.extract_refs(sql) == ["raw.users", "raw.payments"]

    def test_extract_no_refs(self):
        sql = "SELECT 1"
        assert self.resolver.extract_refs(sql) == []

    def test_extract_double_quotes(self):
        sql = 'SELECT * FROM {{ ref("raw.users") }}'
        assert self.resolver.extract_refs(sql) == ["raw.users"]

    def test_extract_with_whitespace(self):
        sql = "SELECT * FROM {{  ref( 'raw.users' )  }}"
        assert self.resolver.extract_refs(sql) == ["raw.users"]

    def test_resolve_sql_no_mapping(self):
        sql = "SELECT * FROM {{ ref('raw.users') }}"
        assert self.resolver.resolve_sql(sql) == "SELECT * FROM raw.users"

    def test_resolve_sql_with_mapping(self):
        sql = "SELECT * FROM {{ ref('raw.users') }}"
        mapping = {"raw.users": "public.raw_users"}
        assert self.resolver.resolve_sql(sql, mapping) == "SELECT * FROM public.raw_users"

    def test_resolve_sql_unknown_mapping_uses_name(self):
        sql = "SELECT * FROM {{ ref('raw.users') }}"
        mapping = {"other": "table"}
        assert self.resolver.resolve_sql(sql, mapping) == "SELECT * FROM raw.users"

    def test_extract_duplicate_refs(self):
        sql = "SELECT * FROM {{ ref('x') }} JOIN {{ ref('x') }}"
        assert self.resolver.extract_refs(sql) == ["x", "x"]

    def test_extract_empty_sql(self):
        assert self.resolver.extract_refs("") == []

    def test_resolve_sql_empty_string(self):
        assert self.resolver.resolve_sql("") == ""

    def test_extract_mismatched_quotes_still_matches(self):
        # Regex allows mixed quotes: opening ' with closing "
        sql = 'SELECT * FROM {{ ref(\'x") }}'
        assert self.resolver.extract_refs(sql) == ["x"]
