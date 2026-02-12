#!/usr/bin/env python3
"""Demo 6: Nested Assets — fields as first-class graph nodes.

Demonstrates how columns (and deeper nested structures) become full Asset
instances that participate in the graph, support selectors, and enable
tree-level fingerprinting.

Run: python demos/06_nested_assets.py
"""

from assets import Asset, AssetField, Registry

# ──────────────────────────────────────────────────────────────
# 1. Define nested asset types
# ──────────────────────────────────────────────────────────────

class Annotation(Asset):
    """Metadata annotation on a column (e.g., PII tag, data quality rule)."""
    level: str = "info"


class Column(Asset):
    """A typed column that can carry annotations."""
    type: str = ""
    pii: bool = False
    annotations: list[Annotation] = AssetField(
        default_factory=list, field_source=True, child_kind="annotation"
    )


class DataModel(Asset):
    """A data model with typed columns."""
    columns: list[Column] = AssetField(
        default_factory=list, field_source=True, child_kind="column"
    )


# ──────────────────────────────────────────────────────────────
# 2. Inline children (auto-flattened on register)
# ──────────────────────────────────────────────────────────────

print("=== Inline Children (auto-flattened) ===\n")

registry = Registry()

registry.register(DataModel(
    name="raw.users",
    kind="source",
    tags=["raw"],
    columns=[
        Column(
            name="user_id",
            type="INTEGER",
        ),
        Column(
            name="email",
            type="VARCHAR",
            pii=True,
            annotations=[
                Annotation(name="pii_flag", description="Contains PII", level="critical"),
                Annotation(name="gdpr", description="Subject to GDPR", level="warning"),
            ],
        ),
        Column(name="created_at", type="TIMESTAMP"),
    ],
))

print(f"Total assets in registry: {len(registry)}")
print(f"Top-level: {[a.name for a in registry.all() if '/' not in a.name]}")
print()

# Show the full hierarchy
for asset in sorted(registry.all(), key=lambda a: a.name):
    indent = "  " * asset.depth
    print(f"{indent}{asset.name} (kind={asset.kind}, depth={asset.depth})")

# ──────────────────────────────────────────────────────────────
# 3. Asset properties: local_name, depth, parent
# ──────────────────────────────────────────────────────────────

print("\n=== Asset Properties ===\n")

email_col = registry.get("raw.users/email")
print(f"Name:       {email_col.name}")
print(f"Local name: {email_col.local_name}")
print(f"Parent:     {email_col.parent}")
print(f"Depth:      {email_col.depth}")
print(f"Kind:       {email_col.kind}")

pii_annot = registry.get("raw.users/email/pii_flag")
print(f"\nName:       {pii_annot.name}")
print(f"Local name: {pii_annot.local_name}")
print(f"Parent:     {pii_annot.parent}")
print(f"Depth:      {pii_annot.depth}")
print(f"Kind:       {pii_annot.kind}")

# ──────────────────────────────────────────────────────────────
# 4. Explicit child registration (alternative path)
# ──────────────────────────────────────────────────────────────

print("\n=== Explicit Child Registration ===\n")

registry.register(Asset(name="staging.users", kind="data_model",
    sql="SELECT * FROM {{ ref('raw.users') }}"))
registry.register(Asset(name="staging.users/user_id", kind="column",
    parent="staging.users"))
registry.register(Asset(name="staging.users/email_clean", kind="column",
    parent="staging.users"))

children = registry.children("staging.users")
print(f"staging.users children: {[c.local_name for c in children]}")

# ──────────────────────────────────────────────────────────────
# 5. Graph: containment vs data-flow edges
# ──────────────────────────────────────────────────────────────

print("\n=== Graph Edge Types ===\n")

graph = registry.graph

# Containment edges (parent → child)
contains = [d for d in registry.dependencies if d.type == "contains"]
print(f"Containment edges ({len(contains)}):")
for d in contains[:6]:
    print(f"  {d.source} -> {d.target}")
if len(contains) > 6:
    print(f"  ... and {len(contains) - 6} more")

# Data-flow edges (ref)
refs = [d for d in registry.dependencies if d.type == "ref"]
print(f"\nData-flow edges ({len(refs)}):")
for d in refs:
    print(f"  {d.source} -> {d.target}")

# Graph methods
print(f"\nTop-level assets: {set(graph.top_level_assets().keys())}")
print(f"Children of raw.users: {graph.children('raw.users')}")
print(f"Data deps of raw.users: {graph.data_dependencies('raw.users')}")

# ──────────────────────────────────────────────────────────────
# 6. Selectors for nested assets
# ──────────────────────────────────────────────────────────────

print("\n=== Nested Selectors ===\n")

# children: — direct children
result = registry.select("children:raw.users")
print(f"children:raw.users -> {result.names}")

# children: — grandchildren
result = registry.select("children:raw.users/email")
print(f"children:raw.users/email -> {result.names}")

# parent: — parent of a child
result = registry.select("parent:raw.users/email")
print(f"parent:raw.users/email -> {result.names}")

# top: — only top-level assets
result = registry.select("top:*")
print(f"top:* -> {result.names}")

# kind: — filter by kind
result = registry.select("kind:column")
print(f"kind:column -> {result.names}")

result = registry.select("kind:annotation")
print(f"kind:annotation -> {result.names}")

# ──────────────────────────────────────────────────────────────
# 7. Tree fingerprinting
# ──────────────────────────────────────────────────────────────

print("\n=== Tree Fingerprinting ===\n")

fp1 = registry.tree_fingerprint("raw.users")
print(f"raw.users tree fingerprint: {fp1[:16]}...")

# Asset-level fingerprint (own fields only)
raw = registry.get("raw.users")
print(f"raw.users asset fingerprint: {raw.fingerprint[:16]}...")
print("(These differ because tree_fingerprint includes children recursively)")

# Compare: changing a deep child changes the tree fingerprint
print(f"\nstaging.users tree fingerprint: {registry.tree_fingerprint('staging.users')[:16]}...")

# ──────────────────────────────────────────────────────────────
# 8. Field introspection (backward compatible)
# ──────────────────────────────────────────────────────────────

print("\n=== Field Introspection ===\n")

raw_users = registry.get("raw.users")
print(f"list_fields(): {raw_users.list_fields()}")

email = raw_users.get_field("email")
print(f"get_field('email'): {email.name} (pii={email.pii})")

# Also works via registry.children()
print("\nregistry.children('raw.users'):")
for child in registry.children("raw.users"):
    print(f"  {child.local_name} (kind={child.kind})")

print("\nDone!")
