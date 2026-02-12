#!/usr/bin/env python3
"""Demo 5: Custom Lineage Resolver — implementing column-level lineage.

Shows how consumers implement their own LineageResolver by subclassing
the abstract base class. This example uses a simple regex-based approach
(in production you'd use sqlglot or similar).

Run: python demos/05_lineage_resolver.py
"""

import re

from assets import Asset, AssetField, FieldMapping, LineageResolver, Registry

# ──────────────────────────────────────────────────────────────
# 1. Define asset types
# ──────────────────────────────────────────────────────────────

class Column(Asset):
    type: str = ""


class DataModel(Asset):
    columns: list[Column] = AssetField(default_factory=list, field_source=True)


# ──────────────────────────────────────────────────────────────
# 2. Implement a custom lineage resolver
# ──────────────────────────────────────────────────────────────

class SimpleLineageResolver(LineageResolver):
    """A simple regex-based lineage resolver for demonstration.

    In production, you would use sqlglot or another SQL parser.
    This resolver handles basic SELECT ... AS ... FROM patterns.
    """

    # Matches: source.column AS alias or just source.column
    _COL_PATTERN = re.compile(
        r"(?:(\w+)\(.*?(\w+)\.(\w+).*?\))"  # FUNC(table.col) -> transform
        r"|(\w+)\.(\w+)"                      # table.col -> direct
    )

    def resolve(self, sql: str, schema: dict[str, list[str]]) -> list[FieldMapping]:
        mappings: list[FieldMapping] = []

        # Build reverse lookup: column_name -> table_name
        col_to_table: dict[str, str] = {}
        for table, columns in schema.items():
            for col in columns:
                col_to_table[col] = table

        # Find SELECT ... AS target_col patterns
        select_match = re.search(r"SELECT\s+(.*?)\s+FROM", sql, re.IGNORECASE | re.DOTALL)
        if not select_match:
            return mappings

        select_clause = select_match.group(1)

        for expr in select_clause.split(","):
            expr = expr.strip()

            # Check for "expr AS alias"
            as_match = re.search(r"\bAS\s+(\w+)", expr, re.IGNORECASE)
            target_col = as_match.group(1) if as_match else None

            # Check for function wrapping: LOWER(TRIM(t.col))
            func_match = re.match(r"(\w+)\(.*?(\w+)\.(\w+).*?\)", expr)
            if func_match:
                transform_name = func_match.group(1)
                source_table_alias = func_match.group(2)
                source_col = func_match.group(3)

                # Resolve table alias -> actual table
                source_table = self._resolve_alias(source_table_alias, schema)
                if source_table and target_col:
                    mappings.append(FieldMapping(
                        source_asset=source_table,
                        source_field=source_col,
                        target_asset="<target>",  # filled by caller
                        target_field=target_col,
                        transform=f"{transform_name}({source_col})",
                    ))
                continue

            # Direct column reference: t.col
            direct_match = re.match(r"(\w+)\.(\w+)", expr)
            if direct_match:
                source_table_alias = direct_match.group(1)
                source_col = direct_match.group(2)
                source_table = self._resolve_alias(source_table_alias, schema)
                output_col = target_col or source_col
                if source_table:
                    mappings.append(FieldMapping(
                        source_asset=source_table,
                        source_field=source_col,
                        target_asset="<target>",
                        target_field=output_col,
                    ))

        return mappings

    @staticmethod
    def _resolve_alias(alias: str, schema: dict[str, list[str]]) -> str | None:
        """Try to resolve a table alias to an actual table name.

        Simple heuristic: if the alias is a known table, return it.
        Otherwise return the first table (works for single-table queries).
        """
        if alias in schema:
            return alias
        tables = list(schema.keys())
        return tables[0] if len(tables) == 1 else None


# ──────────────────────────────────────────────────────────────
# 3. Set up assets
# ──────────────────────────────────────────────────────────────

registry = Registry()

registry.register(DataModel(
    name="raw.users",
    kind="source",
    columns=[
        Column(name="user_id", type="INTEGER"),
        Column(name="email", type="VARCHAR"),
        Column(name="created_at", type="TIMESTAMP"),
    ],
))

registry.register(DataModel(
    name="staging.users",
    kind="data_model",
    sql=(
        "SELECT u.user_id, LOWER(u.email) AS email_clean, u.created_at "
        "FROM {{ ref('raw.users') }} u"
    ),
    columns=[
        Column(name="user_id", type="INTEGER"),
        Column(name="email_clean", type="VARCHAR"),
        Column(name="created_at", type="TIMESTAMP"),
    ],
))

# ──────────────────────────────────────────────────────────────
# 4. Resolve lineage
# ──────────────────────────────────────────────────────────────

print("=== Column-Level Lineage ===\n")

resolver = SimpleLineageResolver()

# Resolve via registry (handles ref resolution and schema building)
lineage = registry.resolve_column_lineage(
    asset_name="staging.users",
    resolver=resolver,
)

print(f"Lineage for staging.users ({len(lineage)} mappings):\n")
for mapping in lineage:
    transform = f" [{mapping.transform}]" if mapping.transform else ""
    print(f"  {mapping.source_asset}.{mapping.source_field} -> {mapping.target_field}{transform}")

# ──────────────────────────────────────────────────────────────
# 5. Show how lineage is on-demand (never automatic)
# ──────────────────────────────────────────────────────────────

print("\n=== Key Point: Lineage is On-Demand ===\n")
print("Lineage resolution NEVER runs during register(), load(), or plan().")
print("The consumer explicitly calls resolve_column_lineage() when needed:")
print("  - Catalog UI: user clicks a column")
print("  - Impact analysis: 'what breaks if I drop this column?'")
print("  - CI/PR review: resolve only changed models")
print("  - PII tracing: resolve lineage for tag:pii assets")

print("\nDone!")
