#!/usr/bin/env python3
"""Demo 5: Custom Dependency Resolver — implementing column-level lineage.

Shows how consumers implement their own DependencyResolver by subclassing
the abstract base class. This example uses a simple regex-based approach
(in production you'd use sqlglot or similar).

Run: python demos/05_lineage_resolver/main.py
"""

import re

from assets import Asset, DependencyResolver, FieldMapping, Registry

# ──────────────────────────────────────────────────────────────
# 1. Define asset types
# ──────────────────────────────────────────────────────────────


class Column(Asset):
    type: str = ""


# ──────────────────────────────────────────────────────────────
# 2. Implement a custom dependency resolver
# ──────────────────────────────────────────────────────────────


class SimpleDependencyResolver(DependencyResolver):
    """A simple regex-based dependency resolver for demonstration.

    In production, you would use sqlglot or another SQL parser.
    This resolver handles basic SELECT ... AS ... FROM patterns.
    """

    def resolve(self, sql: str, schema: dict[str, list[str]]) -> list[FieldMapping]:
        mappings: list[FieldMapping] = []

        # Build reverse lookup: column_name -> table_name
        col_to_table: dict[str, str] = {}
        for table, children in schema.items():
            for col in children:
                col_to_table[col] = table

        # Find SELECT ... AS target_col patterns
        select_match = re.search(
            r"SELECT\s+(.*?)\s+FROM", sql, re.IGNORECASE | re.DOTALL
        )
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
                    mappings.append(
                        FieldMapping(
                            source=f"{source_table}/{source_col}",
                            target=f"<target>/{target_col}",
                            transform=f"{transform_name}({source_col})",
                        )
                    )
                continue

            # Direct column reference: t.col
            direct_match = re.match(r"(\w+)\.(\w+)", expr)
            if direct_match:
                source_table_alias = direct_match.group(1)
                source_col = direct_match.group(2)
                source_table = self._resolve_alias(source_table_alias, schema)
                output_col = target_col or source_col
                if source_table:
                    mappings.append(
                        FieldMapping(
                            source=f"{source_table}/{source_col}",
                            target=f"<target>/{output_col}",
                        )
                    )

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

registry.register(
    Asset(
        id="raw.users",
        type="source",
        children=[
            Column(id="user_id", type="INTEGER"),
            Column(id="email", type="VARCHAR"),
            Column(id="created_at", type="TIMESTAMP"),
        ],
    )
)

registry.register(
    Asset(
        id="staging.users",
        type="data_model",
        sql=(
            "SELECT u.user_id, LOWER(u.email) AS email_clean, u.created_at "
            "FROM raw.users u"
        ),
        depends_on=["raw.users"],
        children=[
            Column(id="user_id", type="INTEGER"),
            Column(id="email_clean", type="VARCHAR"),
            Column(id="created_at", type="TIMESTAMP"),
        ],
    )
)

# ──────────────────────────────────────────────────────────────
# 4. Resolve column-level dependencies
# ──────────────────────────────────────────────────────────────

print("=== Column-Level Dependencies ===\n")

resolver = SimpleDependencyResolver()

# Resolve via registry (handles schema building from upstream assets)
deps = registry.resolve_field_dependency(
    asset_id="staging.users",
    resolver=resolver,
)

print(f"Dependencies for staging.users ({len(deps)} mappings):\n")
for mapping in deps:
    transform = f" [{mapping.transform}]" if mapping.transform else ""
    print(
        "  "
        f"{mapping.source_asset}/{mapping.source_field} "
        f"-> {mapping.target_field}{transform}"
    )

# ──────────────────────────────────────────────────────────────
# 5. Show how dependency resolution is on-demand (never automatic)
# ──────────────────────────────────────────────────────────────

print("\n=== Key Point: Resolution is On-Demand ===\n")
print("Column-level resolution NEVER runs during register() or plan().")
print("The consumer explicitly calls resolve_field_dependency() when needed:")
print("  - Catalog UI: user clicks a column")
print("  - Impact analysis: 'what breaks if I drop this column?'")
print("  - CI/PR review: resolve only changed models")
print("  - PII tracing: resolve lineage for tag:pii assets")

print("\nDone!")
