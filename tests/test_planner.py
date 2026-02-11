"""Tests for Plan model."""

from assets import Plan
from assets.engine.differ import Change, ChangeSet


class TestPlan:
    def test_no_changes(self):
        plan = Plan(environment="dev")
        assert not plan.has_changes
        assert "No changes" in plan.show()

    def test_has_changes(self):
        cs = ChangeSet(
            asset_changes=[
                Change(action="create", asset_name="new", after={"name": "new"})
            ]
        )
        plan = Plan(changeset=cs, environment="dev")
        assert plan.has_changes

    def test_show_create(self):
        cs = ChangeSet(
            asset_changes=[
                Change(action="create", asset_name="new", after={"name": "new"})
            ]
        )
        plan = Plan(changeset=cs, environment="dev")
        output = plan.show()
        assert "+ new" in output
        assert "1 to create" in output

    def test_show_update(self):
        from assets.engine.differ import FieldChange

        cs = ChangeSet(
            asset_changes=[
                Change(
                    action="update",
                    asset_name="test",
                    field_changes=[FieldChange(field="kind", old_value="v1", new_value="v2")],
                )
            ]
        )
        plan = Plan(changeset=cs, environment="dev")
        output = plan.show()
        assert "~ test" in output
        assert "kind" in output

    def test_show_delete(self):
        cs = ChangeSet(
            asset_changes=[
                Change(action="delete", asset_name="old", before={"name": "old"})
            ]
        )
        plan = Plan(changeset=cs, environment="dev")
        output = plan.show()
        assert "- old" in output

    def test_show_mixed(self):
        cs = ChangeSet(
            asset_changes=[
                Change(action="create", asset_name="new"),
                Change(action="update", asset_name="mod", field_changes=[]),
                Change(action="delete", asset_name="old"),
            ]
        )
        plan = Plan(changeset=cs, environment="dev")
        output = plan.show()
        assert "3 change(s)" in output
