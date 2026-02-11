"""RefResolver — extract {{ ref('asset_name') }} patterns from SQL."""

from __future__ import annotations

import re


class RefResolver:
    """Fast regex-based reference extraction from SQL templates."""

    REF_PATTERN = re.compile(r"\{\{\s*ref\(\s*['\"]([^'\"]+)['\"]\s*\)\s*\}\}")

    def extract_refs(self, sql: str) -> list[str]:
        """Return list of referenced asset names from SQL."""
        return self.REF_PATTERN.findall(sql)

    def resolve_sql(self, sql: str, mapping: dict[str, str] | None = None) -> str:
        """Replace {{ ref('x') }} with actual table names.

        Args:
            sql: SQL template with ref() calls.
            mapping: Optional name → table_name mapping. If None, uses the asset name as-is.
        """

        def replacer(match: re.Match[str]) -> str:
            name = match.group(1)
            return mapping.get(name, name) if mapping else name

        return self.REF_PATTERN.sub(replacer, sql)
