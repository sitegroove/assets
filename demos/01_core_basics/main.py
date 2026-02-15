#!/usr/bin/env python3
"""Demo 1: Core Basics — defining assets, fingerprinting, and graph queries.

Uses a **microservices catalog** as the domain: services expose
endpoints, and services depend on other services.  No SQL anywhere.

What you will learn:
  - How to define custom Asset subclasses with Pydantic fields
  - How children work (endpoints nested inside a service)
  - Fingerprinting — what counts and what doesn't
  - The dependency graph and how to traverse it
  - Selectors — filter assets by tag, type, name, or graph position

Run: python demos/01_core_basics/main.py
"""

from typing import cast

from assets import Asset, AssetField, Project

# ──────────────────────────────────────────────────────────────
# 1. Define custom asset types
# ──────────────────────────────────────────────────────────────


class Endpoint(Asset):
    """A single API endpoint exposed by a service."""

    method: str = "GET"
    path: str = ""
    public: bool = False


class Service(Asset):
    """A microservice in our platform.

    ``endpoints`` is marked ``children=True`` so each endpoint
    becomes a nested child asset with an auto-generated path
    like ``endpoints/list-users``.

    ``instance_count`` uses ``fingerprint=False`` — changing it
    will NOT count as a "real" change during plan/apply.
    """

    language: str = "python"
    owner: str = ""
    endpoints: list[Endpoint] = cast(
        list[Endpoint],
        AssetField(default_factory=list, children=True),
    )
    instance_count: int = cast(int, AssetField(default=1, fingerprint=False))


# ──────────────────────────────────────────────────────────────
# 2. Create and register assets
# ──────────────────────────────────────────────────────────────

project = Project()

# A standalone database — no upstream dependencies
project.register(
    Service(
        id="postgres",
        type="database",
        tags=["infra", "storage"],
        language="sql",
    )
)

# User service depends on the database
project.register(
    Service(
        id="user-service",
        type="backend",
        tags=["core", "auth"],
        owner="team-identity",
        depends_on=["postgres"],
        endpoints=[
            Endpoint(id="list-users", method="GET", path="/users"),
            Endpoint(id="create-user", method="POST", path="/users", public=True),
        ],
    )
)

# Order service depends on the database and user-service
project.register(
    Service(
        id="order-service",
        type="backend",
        tags=["core", "commerce"],
        owner="team-commerce",
        depends_on=["postgres", "user-service"],
        endpoints=[
            Endpoint(id="list-orders", method="GET", path="/orders"),
            Endpoint(id="place-order", method="POST", path="/orders", public=True),
        ],
    )
)

# Notification service depends on user-service
project.register(
    Service(
        id="notification-service",
        type="backend",
        tags=["support"],
        owner="team-platform",
        depends_on=["user-service"],
        endpoints=[
            Endpoint(id="send-email", method="POST", path="/notify/email"),
        ],
    )
)

# Web frontend depends on user-service and order-service
project.register(
    Service(
        id="web-frontend",
        type="frontend",
        tags=["web"],
        language="typescript",
        owner="team-web",
        depends_on=["user-service", "order-service"],
    )
)

# ──────────────────────────────────────────────────────────────
# 3. Fingerprinting
# ──────────────────────────────────────────────────────────────

print("=== Fingerprinting ===\n")

svc = project.get("user-service")
assert svc is not None
print(f"user-service fingerprint: {svc.fingerprint[:16]}...")

# instance_count is fingerprint=False — changing it doesn't change the hash
s1 = Service(id="test-svc", instance_count=1)
s2 = Service(id="test-svc", instance_count=100)
print(f"instance_count=1   fingerprint: {s1.fingerprint[:16]}...")
print(f"instance_count=100 fingerprint: {s2.fingerprint[:16]}...")
print(f"Same fingerprint? {s1.fingerprint == s2.fingerprint}")

# Changing a fingerprinted field DOES change the hash
s3 = Service(id="test-svc", language="go")
print(f"language='go'      fingerprint: {s3.fingerprint[:16]}...")
print(f"Same as original? {s1.fingerprint == s3.fingerprint}")

# ──────────────────────────────────────────────────────────────
# 4. Dependencies
# ──────────────────────────────────────────────────────────────

print("\n=== Dependencies ===\n")

order_svc = project.get("order-service")
assert order_svc is not None
print(f"order-service depends_on: {order_svc.depends_on}")

print(f"\nAll dependency edges ({len(project.registry.dependencies)}):")
for dep in project.registry.dependencies:
    print(f"  {dep.source} -> {dep.target}")

# ──────────────────────────────────────────────────────────────
# 5. Graph traversal
# ──────────────────────────────────────────────────────────────

print("\n=== Graph Traversal ===\n")

graph = project.graph
print(f"Graph has {len(graph)} assets")
print(f"Roots (no upstream):   {graph.roots()}")
print(f"Leaves (no downstream): {graph.leaves()}")

print(f"\nAncestors of web-frontend:  {graph.ancestors('web-frontend')}")
print(f"Descendants of postgres:    {graph.descendants('postgres')}")

# Depth-limited traversal
depth1 = graph.ancestors("web-frontend", max_depth=1)
print(f"Ancestors of web-frontend (depth=1): {depth1}")

# Topological sort — a valid execution order
print(f"\nTopological order: {graph.topological_sort()}")

# ──────────────────────────────────────────────────────────────
# 6. Selectors
# ──────────────────────────────────────────────────────────────

print("\n=== Selectors ===\n")

# By tag
core = project.select("tag:core")
print(f"tag:core -> {core.names}")

# By type
backends = project.select("type:backend")
print(f"type:backend -> {backends.names}")

# Wildcard
all_services = project.select("*-service")
print(f"*-service -> {all_services.names}")

# Upstream expansion — the asset plus all its ancestors
upstream = project.select("+web-frontend")
print(f"+web-frontend (all ancestors) -> {upstream.names}")

# Downstream expansion — the asset plus all its descendants
downstream = project.select("postgres+")
print(f"postgres+ (all descendants) -> {downstream.names}")

# Depth-limited
depth1_sel = project.select("postgres+1")
print(f"postgres+1 (descendants depth=1) -> {depth1_sel.names}")

# Intersection (AND)
intersect = project.select("tag:core,type:backend")
print(f"tag:core,type:backend (AND) -> {intersect.names}")

# Exclude
excluded = project.select("type:backend", exclude="tag:support")
print(f"type:backend exclude tag:support -> {excluded.names}")

# ──────────────────────────────────────────────────────────────
# 7. Nested asset introspection (children)
# ──────────────────────────────────────────────────────────────

print("\n=== Nested Asset Introspection ===\n")

user_svc = project.get("user-service")
assert user_svc is not None
kids = user_svc.children()
print(f"user-service children: {[c.id for c in kids]}")

create_ep = user_svc.child("endpoints/create-user")
assert create_ep is not None
create_ep = cast(Endpoint, create_ep)
print(f"  create-user method: {create_ep.method}")
print(f"  create-user path:   {create_ep.path}")
print(f"  create-user public: {create_ep.public}")

# Child not found returns None
missing = user_svc.child("endpoints/nonexistent")
print(f"  child('endpoints/nonexistent'): {missing}")

print("\nDone!")
