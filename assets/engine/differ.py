"""Differ — fingerprint-first diffing with deep field-level comparison."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel

from assets.core.asset import Asset
from assets.state.models import AssetState


class FieldChange(BaseModel):
    """A single field-level change."""

    field: str
    old_value: Any = None
    new_value: Any = None


class Change(BaseModel):
    """A single asset or dependency change."""

    action: Literal["create", "update", "delete"]
    asset_name: str
    before: dict[str, Any] | None = None
    after: dict[str, Any] | None = None
    before_fingerprint: str | None = None
    after_fingerprint: str | None = None
    field_changes: list[FieldChange] = []


class ChangeSet(BaseModel):
    """Collection of changes for a plan."""

    asset_changes: list[Change] = []
    dependency_changes: list[Change] = []


class Differ:
    """Compare desired assets against current state.

    Uses fingerprints for fast skip, deep diff only when fingerprints differ.
    """

    def diff(self, desired: list[Asset], current: dict[str, AssetState]) -> ChangeSet:
        asset_changes: list[Change] = []
        desired_names: set[str] = set()

        for asset in desired:
            desired_names.add(asset.name)
            existing = current.get(asset.name)

            if existing is None or existing.deleted:
                # New asset
                asset_changes.append(
                    Change(
                        action="create",
                        asset_name=asset.name,
                        after=asset.model_dump(),
                        after_fingerprint=asset.fingerprint,
                    )
                )
            elif existing.fingerprint != asset.fingerprint:
                # Changed asset — compute deep diff
                field_changes = self._deep_diff(existing.data, asset.model_dump())
                asset_changes.append(
                    Change(
                        action="update",
                        asset_name=asset.name,
                        before=existing.data,
                        after=asset.model_dump(),
                        before_fingerprint=existing.fingerprint,
                        after_fingerprint=asset.fingerprint,
                        field_changes=field_changes,
                    )
                )
            # else: fingerprints match, skip

        # Deletions: assets in state but not in desired
        for name, state in current.items():
            if name not in desired_names and not state.deleted:
                asset_changes.append(
                    Change(
                        action="delete",
                        asset_name=name,
                        before=state.data,
                        before_fingerprint=state.fingerprint,
                    )
                )

        return ChangeSet(asset_changes=asset_changes)

    def _deep_diff(
        self, old: dict[str, Any], new: dict[str, Any]
    ) -> list[FieldChange]:
        """Field-by-field comparison between old and new asset dicts."""
        changes: list[FieldChange] = []
        all_keys = set(old.keys()) | set(new.keys())
        skip = {"fingerprint"}

        for key in sorted(all_keys):
            if key in skip:
                continue
            old_val = old.get(key)
            new_val = new.get(key)
            if old_val != new_val:
                changes.append(FieldChange(field=key, old_value=old_val, new_value=new_val))

        return changes
