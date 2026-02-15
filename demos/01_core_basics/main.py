#!/usr/bin/env python3
"""Demo 1: Core Basics — defining assets, fingerprinting, and graph queries.

Run: python demos/01_core_basics/main.py
"""

from assets import Asset, AssetField, Assets

# ──────────────────────────────────────────────────────────────
# 1. Define a custom asset type
# ──────────────────────────────────────────────────────────────


class Column(Asset):
    type: str = ""
    description: str = ""
    pii: bool = False


class DataModel(Asset):
    """A data model with a non-fingerprinted row_count."""

    row_count: int = AssetField(default=0, fingerprint=False)


# ──────────────────────────────────────────────────────────────
# 2. Create and register assets
# ──────────────────────────────────────────────────────────────

project = Assets()

# Raw source — no SQL, no dependencies
project.register(
    DataModel(
        id="raw.users",
        type="source",
        tags=["raw", "pii"],
        children=[
            Column(id="user_id", type="INTEGER"),
            Column(id="email", type="VARCHAR", pii=True),
            Column(id="created_at", type="TIMESTAMP"),
        ],
    )
)

project.register(
    DataModel(
        id="raw.payments",
        type="source",
        tags=["raw", "finance"],
        children=[
            Column(id="payment_id", type="INTEGER"),
            Column(id="user_id", type="INTEGER"),
            Column(id="amount", type="DECIMAL"),
        ],
    )
)

# Staging model — depends on raw.users (consumer sets depends_on explicitly)
project.register(
    DataModel(
        id="staging.users",
        type="data_model",
        tags=["staging", "pii"],
        sql=(
            "SELECT u.user_id, LOWER(TRIM(u.email)) AS email_clean, u.created_at "
            "FROM raw.users u "
            "WHERE u.created_at IS NOT NULL"
        ),
        depends_on=["raw.users"],
        children=[
            Column(id="user_id", type="INTEGER"),
            Column(
                id="email_clean",
                type="VARCHAR",
                description="Lowercased, trimmed",
                pii=True,
            ),
            Column(id="created_at", type="TIMESTAMP"),
        ],
    )
)

# Mart model — depends on both staging.users and raw.payments
project.register(
    DataModel(
        id="mart.user_spending",
        type="data_model",
        tags=["mart", "finance"],
        sql=(
            "SELECT u.user_id, u.email_clean, SUM(p.amount) AS total_spent "
            "FROM staging.users u "
            "JOIN raw.payments p ON u.user_id = p.user_id "
            "GROUP BY u.user_id, u.email_clean"
        ),
        depends_on=["staging.users", "raw.payments"],
        children=[
            Column(id="user_id", type="INTEGER"),
            Column(id="email_clean", type="VARCHAR"),
            Column(id="total_spent", type="DECIMAL"),
        ],
    )
)

# ──────────────────────────────────────────────────────────────
# 3. Fingerprinting
# ──────────────────────────────────────────────────────────────

print("=== Fingerprinting ===\n")

user_model = project.get("raw.users")
print(f"raw.users fingerprint: {user_model.fingerprint[:16]}...")

# row_count is fingerprint=False — changing it doesn't change the hash
m1 = DataModel(id="test", row_count=0)
m2 = DataModel(id="test", row_count=999_999)
print(f"row_count=0 fingerprint:      {m1.fingerprint[:16]}...")
print(f"row_count=999999 fingerprint:  {m2.fingerprint[:16]}...")
print(f"Same fingerprint? {m1.fingerprint == m2.fingerprint}")

# But changing a fingerprinted field does change the hash
m3 = DataModel(id="test", type="changed")
print(f"type='changed' fingerprint:    {m3.fingerprint[:16]}...")
print(f"Same as original? {m1.fingerprint == m3.fingerprint}")

# ──────────────────────────────────────────────────────────────
# 4. Dependencies (consumer-declared)
# ──────────────────────────────────────────────────────────────

print("\n=== Dependencies ===\n")

staging_users = project.get("staging.users")
print(f"staging.users depends_on: {staging_users.depends_on}")

mart = project.get("mart.user_spending")
print(f"mart.user_spending depends_on: {mart.depends_on}")

print(f"\nAll dependencies ({len(project.registry.dependencies)}):")
for dep in project.registry.dependencies:
    print(f"  {dep.source} -> {dep.target} (type={dep.type})")

# ──────────────────────────────────────────────────────────────
# 5. Graph traversal
# ──────────────────────────────────────────────────────────────

print("\n=== Graph Traversal ===\n")

graph = project.graph
print(f"Graph has {len(graph)} assets")
print(f"Roots (no upstream): {graph.roots()}")
print(f"Leaves (no downstream): {graph.leaves()}")

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
pii = project.select("tag:pii")
print(f"tag:pii -> {pii.names}")

# By kind
sources = project.select("type:source")
print(f"type:source -> {sources.names}")

# Wildcard
raw = project.select("raw.*")
print(f"raw.* -> {raw.names}")

# Upstream expansion
upstream = project.select("+mart.user_spending")
print(f"+mart.user_spending (asset + all ancestors) -> {upstream.names}")

# Downstream expansion
downstream = project.select("raw.users+")
print(f"raw.users+ (asset + all descendants) -> {downstream.names}")

# Depth-limited
depth1 = project.select("raw.users+1")
print(f"raw.users+1 (descendants depth=1) -> {depth1.names}")

# Intersection
intersect = project.select("tag:pii,type:data_model")
print(f"tag:pii,type:data_model (AND) -> {intersect.names}")

# ──────────────────────────────────────────────────────────────
# 7. Nested asset introspection
# ──────────────────────────────────────────────────────────────

print("\n=== Nested Asset Introspection ===\n")

staging = project.get("staging.users")
print(f"staging.users children: {staging.list_children()}")

email_col = staging.get_child("email_clean")
print(f"  email_clean.type: {email_col.type}")
print(f"  email_clean.pii: {email_col.pii}")
print(f"  email_clean.description: {email_col.description}")

# Child not found
missing = staging.get_child("nonexistent")
print(f"  get_child('nonexistent'): {missing}")

print("\nDone!")
