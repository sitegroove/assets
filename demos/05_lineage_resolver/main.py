#!/usr/bin/env python3
"""Demo 5: Custom Dependency Resolver — field-level API contract mapping.

Shows how consumers implement their own DependencyResolver to track
field-level dependencies between assets.  The domain is **API
endpoints**: we map response fields of one service to request fields
of its downstream consumer.  No SQL involved.

What you will learn:
  - The DependencyResolver protocol (abstract base class)
  - How to implement ``resolve()`` for your domain
  - How the library builds upstream schemas for your resolver
  - FieldMapping objects and how to inspect them

Run: python demos/05_lineage_resolver/main.py
"""

from typing import cast

from assets import Asset, AssetField, DependencyResolver, FieldMapping, Project

# ──────────────────────────────────────────────────────────────
# 1. Define asset types
# ──────────────────────────────────────────────────────────────


class Field(Asset):
    """A single field in an API request or response."""

    data_type: str = "string"
    required: bool = True


class ApiEndpoint(Asset):
    """An API endpoint that produces (response) and consumes (request) fields.

    ``response_fields`` are the child assets — they describe what this
    endpoint outputs.  ``request_mapping`` is a simple dict that says
    "my request field X comes from upstream field Y".
    """

    method: str = "GET"
    path: str = ""
    response_fields: list[Field] = cast(
        list[Field],
        AssetField(default_factory=list, children=True),
    )
    request_mapping: dict[str, str] = cast(
        dict[str, str],
        AssetField(default_factory=dict),
    )


# ──────────────────────────────────────────────────────────────
# 2. Implement a custom dependency resolver
# ──────────────────────────────────────────────────────────────


class ApiContractResolver(DependencyResolver):
    """Maps request fields of a downstream endpoint to response
    fields of its upstream endpoints.

    The ``request_mapping`` on each endpoint says:
        ``{"my_field": "upstream_field_name"}``

    The resolver looks up which upstream endpoint provides that
    field and produces a FieldMapping.
    """

    def resolve(self, asset: Asset, schema: dict[str, list[str]]) -> list[FieldMapping]:
        mappings: list[FieldMapping] = []
        mapping = getattr(asset, "request_mapping", None)
        if not mapping:
            return mappings

        # Build reverse lookup: field_name -> "upstream_id/response_fields/field"
        field_to_source: dict[str, str] = {}
        for upstream_id, namespaced_children in schema.items():
            for child_path in namespaced_children:
                # child_path looks like "response_fields/user_id"
                _, _, field_name = child_path.rpartition("/")
                field_to_source[field_name] = f"{upstream_id}/{child_path}"

        for my_field, upstream_field in mapping.items():
            source = field_to_source.get(upstream_field)
            if source:
                mappings.append(
                    FieldMapping(
                        source=source,
                        target=f"<target>/response_fields/{my_field}",
                    )
                )

        return mappings


# ──────────────────────────────────────────────────────────────
# 3. Set up assets
# ──────────────────────────────────────────────────────────────

project = Project()

# Upstream: user-service GET /users/{id}
project.register(
    ApiEndpoint(
        id="user-service.get-user",
        type="api",
        tags=["core", "auth"],
        method="GET",
        path="/users/{id}",
        response_fields=[
            Field(id="user_id", data_type="integer"),
            Field(id="email", data_type="string"),
            Field(id="display_name", data_type="string"),
            Field(id="created_at", data_type="datetime"),
        ],
    )
)

# Upstream: order-service GET /orders
project.register(
    ApiEndpoint(
        id="order-service.list-orders",
        type="api",
        tags=["core", "commerce"],
        method="GET",
        path="/orders",
        depends_on=["user-service.get-user"],
        response_fields=[
            Field(id="order_id", data_type="integer"),
            Field(id="user_id", data_type="integer"),
            Field(id="total", data_type="decimal"),
            Field(id="status", data_type="string"),
        ],
        # This endpoint takes user_id from the user-service
        request_mapping={"user_id": "user_id"},
    )
)

# Downstream: dashboard consumes both services
project.register(
    ApiEndpoint(
        id="dashboard.user-summary",
        type="bff",
        tags=["frontend", "aggregation"],
        method="GET",
        path="/dashboard/user/{id}",
        depends_on=["user-service.get-user", "order-service.list-orders"],
        response_fields=[
            Field(id="name", data_type="string"),
            Field(id="email", data_type="string"),
            Field(id="total_orders", data_type="integer"),
        ],
        request_mapping={
            "name": "display_name",  # from user-service
            "email": "email",  # from user-service
            "total_orders": "order_id",  # from order-service (count)
        },
    )
)

# ──────────────────────────────────────────────────────────────
# 4. Resolve field-level dependencies
# ──────────────────────────────────────────────────────────────

print("=== Field-Level Dependencies ===\n")

project.add_resolver("api-contracts", ApiContractResolver())

# Resolve for the dashboard endpoint
deps = project.resolve("api-contracts", asset_id="dashboard.user-summary")

print(f"dashboard.user-summary has {len(deps)} field mappings:\n")
for m in deps:
    print(f"  {m.source_asset}/{m.source_field}")
    print(f"    -> {m.target_field}")
    print()

# Resolve for order-service (simpler — only one upstream)
deps2 = project.resolve("api-contracts", asset_id="order-service.list-orders")

print(f"order-service.list-orders has {len(deps2)} field mappings:\n")
for m in deps2:
    print(f"  {m.source_asset}/{m.source_field} -> {m.target_field}")

# ──────────────────────────────────────────────────────────────
# 5. Graph exploration
# ──────────────────────────────────────────────────────────────

print("\n=== Graph ===\n")

graph = project.graph
print(f"Assets: {len(graph)}")
print(f"Roots:  {graph.roots()}")
print(f"Leaves: {graph.leaves()}")
print(f"Order:  {graph.topological_sort()}")

# Impact analysis: what is affected if user-service changes?
impacted = graph.descendants("user-service.get-user")
print(f"\nImpact of user-service.get-user change: {impacted}")

print("\nDone!")
