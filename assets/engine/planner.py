"""Plan — the result of diffing desired vs. current state."""

from __future__ import annotations

from datetime import datetime, timezone

from pydantic import BaseModel, Field

from assets.engine.differ import ChangeSet


class Plan(BaseModel):
    """Represents a set of changes to be applied to an environment."""

    changeset: ChangeSet = Field(default_factory=ChangeSet)
    environment: str = ""
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def has_changes(self) -> bool:
        return bool(self.changeset.asset_changes or self.changeset.dependency_changes)

    @property
    def changed_ids(self) -> set[str]:
        """All asset IDs with any change (create, update, or delete)."""
        return {c.asset_id for c in self.changeset.asset_changes}

    @property
    def created_ids(self) -> set[str]:
        """Asset IDs that will be created."""
        return {
            c.asset_id for c in self.changeset.asset_changes if c.action == "create"
        }

    @property
    def updated_ids(self) -> set[str]:
        """Asset IDs that will be updated."""
        return {
            c.asset_id for c in self.changeset.asset_changes if c.action == "update"
        }

    @property
    def deleted_ids(self) -> set[str]:
        """Asset IDs that will be deleted."""
        return {
            c.asset_id for c in self.changeset.asset_changes if c.action == "delete"
        }

    def __repr__(self) -> str:
        n = len(self.changeset.asset_changes)
        return f"Plan(environment={self.environment!r}, changes={n})"

    def show(self) -> str:
        """Pretty-print the plan."""
        if not self.has_changes:
            return f"No changes detected for environment '{self.environment}'."

        lines: list[str] = []
        lines.append(f"Plan for environment: {self.environment}")
        lines.append(f"Created at: {self.created_at.isoformat()}")
        lines.append("")

        creates = [c for c in self.changeset.asset_changes if c.action == "create"]
        updates = [c for c in self.changeset.asset_changes if c.action == "update"]
        deletes = [c for c in self.changeset.asset_changes if c.action == "delete"]

        if creates:
            lines.append(f"  + {len(creates)} to create:")
            for c in creates:
                lines.append(f"    + {c.asset_id}")

        if updates:
            lines.append(f"  ~ {len(updates)} to update:")
            for c in updates:
                changed_fields = ", ".join(fc.field for fc in c.field_changes)
                lines.append(f"    ~ {c.asset_id} ({changed_fields})")

        if deletes:
            lines.append(f"  - {len(deletes)} to delete:")
            for c in deletes:
                lines.append(f"    - {c.asset_id}")

        lines.append("")
        total = len(creates) + len(updates) + len(deletes)
        lines.append(f"Total: {total} change(s)")
        return "\n".join(lines)
