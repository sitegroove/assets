#!/usr/bin/env python3
"""Demo 1: Core Basics — defining assets, fingerprinting, and graph queries.

Run: python demos/01_core_basics.py
"""

from assets import Asset, AssetField, Registry

# ──────────────────────────────────────────────────────────────
# 1. Define a custom asset type
# ──────────────────────────────────────────────────────────────

class Column(Asset):
    type: str = ""
    description: str = ""
    pii: bool = False


class DataModel(Asset):
    """A data model with typed columns and a non-fingerprinted row_count."""

    columns: list[Column] = AssetField(default_factory=list, field_source=True)
    row_count: int = AssetField(default=0, fingerprint=False)


# ──────────────────────────────────────────────────────────────
# 2. Create and register assets
# ──────────────────────────────────────────────────────────────

registry = Registry()

# Raw source — no SQL, no dependencies
registry.register(DataModel(
    name="raw.users",
    kind="source",
    tags=["raw", "pii"],
    columns=[
        Column(name="user_id", type="INTEGER"),
        Column(name="email", type="VARCHAR", pii=True),
        Column(name="created_at", type="TIMESTAMP"),
    ],
))

registry.register(DataModel(
    name="raw.payments",
    kind="source",
    tags=["raw", "finance"],
    columns=[
        Column(name="payment_id", type="INTEGER"),
        Column(name="user_id", type="INTEGER"),
        Column(name="amount", type="DECIMAL"),
    ],
))

# Staging model — depends on raw.users via {{ ref() }}
registry.register(DataModel(
    name="staging.users",
    kind="data_model",
    tags=["staging", "pii"],
    sql=(
        "SELECT u.user_id, LOWER(TRIM(u.email)) AS email_clean, u.created_at "
        "FROM {{ ref('raw.users') }} u "
        "WHERE u.created_at IS NOT NULL"
    ),
    columns=[
        Column(name="user_id", type="INTEGER"),
        Column(name="email_clean", type="VARCHAR", description="Lowercased, trimmed", pii=True),
        Column(name="created_at", type="TIMESTAMP"),
    ],
))

# Mart model — depends on both staging.users and raw.payments
registry.register(DataModel(
    name="mart.user_spending",
    kind="data_model",
    tags=["mart", "finance"],
    sql=(
        "SELECT u.user_id, u.email_clean, SUM(p.amount) AS total_spent "
        "FROM {{ ref('staging.users') }} u "
        "JOIN {{ ref('raw.payments') }} p ON u.user_id = p.user_id "
        "GROUP BY u.user_id, u.email_clean"
    ),
    columns=[
        Column(name="user_id", type="INTEGER"),
        Column(name="email_clean", type="VARCHAR"),
        Column(name="total_spent", type="DECIMAL"),
    ],
))

# ──────────────────────────────────────────────────────────────
# 3. Fingerprinting
# ──────────────────────────────────────────────────────────────

print("=== Fingerprinting ===\n")

user_model = registry.get("raw.users")
print(f"raw.users fingerprint: {user_model.fingerprint[:16]}...")

# row_count is fingerprint=False — changing it doesn't change the hash
m1 = DataModel(name="test", row_count=0)
m2 = DataModel(name="test", row_count=999_999)
print(f"row_count=0 fingerprint:      {m1.fingerprint[:16]}...")
print(f"row_count=999999 fingerprint:  {m2.fingerprint[:16]}...")
print(f"Same fingerprint? {m1.fingerprint == m2.fingerprint}")

# But changing a fingerprinted field does change the hash
m3 = DataModel(name="test", kind="changed")
print(f"kind='changed' fingerprint:    {m3.fingerprint[:16]}...")
print(f"Same as original? {m1.fingerprint == m3.fingerprint}")

# ──────────────────────────────────────────────────────────────
# 4. Automatic dependency extraction
# ──────────────────────────────────────────────────────────────

print("\n=== Dependencies ===\n")

staging_users = registry.get("staging.users")
print(f"staging.users depends_on: {staging_users.depends_on}")

mart = registry.get("mart.user_spending")
print(f"mart.user_spending depends_on: {mart.depends_on}")

ref_deps = [d for d in registry.dependencies if d.type == "ref"]
print(f"\nRef dependencies ({len(ref_deps)}):")
for dep in ref_deps:
    print(f"  {dep.source} -> {dep.target} (type={dep.type})")

# ──────────────────────────────────────────────────────────────
# 5. Graph traversal
# ──────────────────────────────────────────────────────────────

print("\n=== Graph Traversal ===\n")

graph = registry.graph
print(f"Graph has {len(graph)} assets ({len(graph.top_level_assets())} top-level)")
print(f"Roots (no upstream): {graph.roots()}")
top_leaves = {n for n in graph.leaves() if "/" not in n}
print(f"Top-level leaves (no downstream data deps): {top_leaves}")

print(f"\nAncestors of mart.user_spending: {graph.ancestors('mart.user_spending')}")
print(f"Descendants of raw.users: {graph.descendants('raw.users')}")

# Depth-limited traversal
depth1_ancestors = graph.ancestors("mart.user_spending", max_depth=1)
print(f"Ancestors of mart.user_spending (depth=1): {depth1_ancestors}")

# Topological sort
print(f"\nTopological order: {graph.topological_sort()}")

# ──────────────────────────────────────────────────────────────
# 6. Selectors
# ──────────────────────────────────────────────────────────────

print("\n=== Selectors ===\n")

# By tag
pii = registry.select("tag:pii")
print(f"tag:pii -> {pii.names}")

# By kind
sources = registry.select("kind:source")
print(f"kind:source -> {sources.names}")

# Wildcard
raw = registry.select("raw.*")
print(f"raw.* -> {raw.names}")

# Upstream expansion
upstream = registry.select("+mart.user_spending")
print(f"+mart.user_spending (asset + all ancestors) -> {upstream.names}")

# Downstream expansion
downstream = registry.select("raw.users+")
print(f"raw.users+ (asset + all descendants) -> {downstream.names}")

# Depth-limited
depth1 = registry.select("raw.users+1")
print(f"raw.users+1 (descendants depth=1) -> {depth1.names}")

# Intersection
intersect = registry.select("tag:pii,kind:data_model")
print(f"tag:pii,kind:data_model (AND) -> {intersect.names}")

# ──────────────────────────────────────────────────────────────
# 7. Field introspection
# ──────────────────────────────────────────────────────────────

print("\n=== Field Introspection ===\n")

staging = registry.get("staging.users")
print(f"staging.users fields: {staging.list_fields()}")

email_col = staging.get_field("email_clean")
print(f"  email_clean.type: {email_col.type}")
print(f"  email_clean.pii: {email_col.pii}")
print(f"  email_clean.description: {email_col.description}")

# Field not found
missing = staging.get_field("nonexistent")
print(f"  get_field('nonexistent'): {missing}")

# ──────────────────────────────────────────────────────────────
# 8. Nested assets
# ──────────────────────────────────────────────────────────────

print("\n=== Nested Assets ===\n")

# Children are full graph nodes
children = registry.children("staging.users")
print(f"staging.users children: {[c.name for c in children]}")

for child in children:
    print(f"  {child.name}: local_name={child.local_name}, depth={child.depth}, kind={child.kind}")

# Containment deps
contains_deps = [d for d in registry.dependencies if d.type == "contains"]
print(f"\nContainment dependencies: {len(contains_deps)}")
for dep in contains_deps[:5]:
    print(f"  {dep.source} -> {dep.target}")
if len(contains_deps) > 5:
    print(f"  ... and {len(contains_deps) - 5} more")

# Tree fingerprint
print(f"\nTree fingerprint for raw.users: {registry.tree_fingerprint('raw.users')[:16]}...")
print(f"Tree fingerprint for staging.users: {registry.tree_fingerprint('staging.users')[:16]}...")

# New selectors
print("\n=== Nested Selectors ===\n")
child_result = registry.select("children:raw.users")
print(f"children:raw.users -> {child_result.names}")

parent_result = registry.select("parent:raw.users/email")
print(f"parent:raw.users/email -> {parent_result.names}")

top_result = registry.select("top:*")
print(f"top:* -> {top_result.names}")

print("\nDone!")
