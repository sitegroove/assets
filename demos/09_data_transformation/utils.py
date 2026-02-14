"""Jinja2 rendering and sqlglot column-level lineage utilities.

Consumer-owned helpers — the ``assets`` library never prescribes how
SQL is parsed or rendered.  These utilities bridge Jinja2 templating
and sqlglot column lineage into the library's ``DependencyResolver``
protocol.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from assets import DependencyResolver, FieldMapping

# ── Optional dependency guards ────────────────────────────────────────

try:
    import jinja2

    HAS_JINJA2 = True
except ImportError:  # pragma: no cover
    HAS_JINJA2 = False

try:
    import sqlglot
    from sqlglot.lineage import lineage as sqlglot_lineage

    HAS_SQLGLOT = True
except ImportError:  # pragma: no cover
    HAS_SQLGLOT = False


# ── Jinja2 rendering ─────────────────────────────────────────────────


@dataclass
class RenderedSQL:
    """Result of rendering a Jinja2 SQL template."""

    sql: str
    refs: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)


class JinjaRenderer:
    """Renders dbt-style SQL templates using Jinja2.

    Supports ``{{ ref('model') }}``, ``{{ source('src', 'table') }}``,
    ``{{ var('key') }}``, and custom macros loaded from a directory.

    Args:
        source_mapping: Maps ``(source_name, table_name)`` to an asset
            ID so that ``{{ source(...) }}`` renders to a valid
            identifier.
        variables: Variable dict available via ``{{ var('key') }}``.
        macro_paths: Directories containing ``.sql`` macro files.
    """

    def __init__(
        self,
        source_mapping: dict[tuple[str, str], str] | None = None,
        variables: dict[str, Any] | None = None,
        macro_paths: list[Path] | None = None,
    ) -> None:
        if not HAS_JINJA2:
            raise ImportError(
                "jinja2 is required for JinjaRenderer. "
                "Install it with: pip install jinja2"
            )

        self._source_mapping = source_mapping or {}
        self._variables = variables or {}
        self._refs: list[str] = []
        self._sources: list[str] = []

        # Load macro definitions from .sql files
        self._macro_source = self._load_macros(macro_paths or [])

        # Reuse a single Jinja2 Environment (avoid per-render overhead)
        self._env = jinja2.Environment(
            undefined=jinja2.StrictUndefined,
        )
        self._env.globals["ref"] = self._ref
        self._env.globals["source"] = self._source
        self._env.globals["var"] = self._var

    # ── Public API ────────────────────────────────────────────────────

    def render(self, sql_template: str) -> RenderedSQL:
        """Render a SQL template, capturing ref/source dependencies."""
        self._refs = []
        self._sources = []

        full_template = self._macro_source + "\n" + sql_template

        template = self._env.from_string(full_template)
        rendered = template.render()

        return RenderedSQL(
            sql=rendered.strip(),
            refs=list(dict.fromkeys(self._refs)),
            sources=list(dict.fromkeys(self._sources)),
        )

    # ── Template functions ────────────────────────────────────────────

    def _ref(self, model_name: str) -> str:
        """Handle ``{{ ref('model_name') }}``."""
        self._refs.append(model_name)
        return model_name

    def _source(self, source_name: str, table_name: str) -> str:
        """Handle ``{{ source('source_name', 'table_name') }}``."""
        key = (source_name, table_name)
        asset_id = self._source_mapping.get(key)
        if asset_id is None:
            raise ValueError(
                f"Unknown source: source('{source_name}', "
                f"'{table_name}'). "
                f"Known sources: {list(self._source_mapping.keys())}"
            )
        self._sources.append(asset_id)
        return asset_id

    def _var(self, key: str, default: Any = None) -> Any:
        """Handle ``{{ var('key') }}``."""
        if key not in self._variables and default is None:
            raise ValueError(
                f"Undefined variable: '{key}'. "
                f"Known variables: {list(self._variables.keys())}"
            )
        return self._variables.get(key, default)

    # ── Macro loading ─────────────────────────────────────────────────

    @staticmethod
    def _load_macros(macro_paths: list[Path]) -> str:
        """Concatenate all .sql macro files into a single string."""
        parts: list[str] = []
        for directory in macro_paths:
            if not directory.is_dir():
                continue
            for sql_file in sorted(directory.glob("*.sql")):
                parts.append(sql_file.read_text())
        return "\n".join(parts)


# ── sqlglot column-level lineage ──────────────────────────────────────


class ColumnLineageResolver(DependencyResolver):
    """Column-level dependency resolver using ``sqlglot.lineage()``.

    Traces columns through CTEs, subqueries, JOINs, and aggregations.
    Falls back gracefully when sqlglot is not installed.
    """

    def resolve(self, sql: str, schema: dict[str, list[str]]) -> list[FieldMapping]:
        """Parse SQL and trace column dependencies."""
        if not HAS_SQLGLOT:
            return []

        mappings: list[FieldMapping] = []

        # Build sqlglot schema: {table: {col: "VARCHAR"}}
        sg_schema: dict[str, dict[str, str]] = {}
        for table, children in schema.items():
            sg_schema[table] = {col: "VARCHAR" for col in children}

        try:
            parsed = sqlglot.parse_one(sql)
        except Exception:
            return mappings

        output_cols = self._extract_output_columns(parsed, sg_schema)

        for col_name in output_cols:
            try:
                node = sqlglot_lineage(col_name, sql, schema=sg_schema)
                self._walk_lineage(node, col_name, mappings)
            except Exception:
                continue

        return mappings

    # ── Internal helpers ──────────────────────────────────────────────

    @staticmethod
    def _extract_output_columns(
        parsed: Any,
        sg_schema: dict[str, dict[str, str]],
    ) -> list[str]:
        """Extract output column names from a parsed SQL."""
        output_cols: list[str] = []
        has_star = False

        for sel in parsed.selects:
            col_name = sel.alias_or_name
            if col_name == "*":
                has_star = True
            elif col_name:
                output_cols.append(col_name)

        if has_star and not output_cols:
            try:
                from sqlglot.optimizer.qualify_columns import (
                    qualify_columns,
                )
                from sqlglot.optimizer.qualify_tables import (
                    qualify_tables,
                )

                qualified = qualify_columns(
                    qualify_tables(parsed, schema=sg_schema),
                    schema=sg_schema,
                )
                for sel in qualified.selects:
                    name = sel.alias_or_name
                    if name and name != "*":
                        output_cols.append(name)
            except Exception:
                seen: set[str] = set()
                for cols in sg_schema.values():
                    for c in cols:
                        if c not in seen:
                            output_cols.append(c)
                            seen.add(c)

        return output_cols

    @staticmethod
    def _walk_lineage(
        node: Any,
        target_col: str,
        mappings: list[FieldMapping],
    ) -> None:
        """Recursively walk the lineage tree to leaf sources."""
        if not node.downstream:
            src_table = ""
            if hasattr(node.source, "this") and hasattr(node.source.this, "this"):
                src_table = str(node.source.this.this)

            src_col = node.name.split(".")[-1] if "." in node.name else node.name

            transform: str | None = None
            expr_sql = str(node.expression) if node.expression else ""
            for fn in (
                "SUM",
                "AVG",
                "COUNT",
                "MIN",
                "MAX",
                "COALESCE",
            ):
                if fn in expr_sql.upper():
                    transform = fn
                    break

            if src_table and src_table != "*":
                mappings.append(
                    FieldMapping(
                        source=f"{src_table}/{src_col}",
                        target=f"<target>/{target_col}",
                        transform=transform,
                    )
                )
        else:
            for child in node.downstream:
                ColumnLineageResolver._walk_lineage(child, target_col, mappings)
