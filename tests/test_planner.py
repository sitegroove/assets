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
            asset_changes=[Change(action="create", asset_id="new", after={"id": "new"})]
        )
        plan = Plan(changeset=cs, environment="dev")
        assert plan.has_changes

    def test_show_create(self):
        cs = ChangeSet(
            asset_changes=[Change(action="create", asset_id="new", after={"id": "new"})]
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
                    asset_id="test",
                    field_changes=[
                        FieldChange(field="type", old_value="v1", new_value="v2")
                    ],
                )
            ]
        )
        plan = Plan(changeset=cs, environment="dev")
        output = plan.show()
        assert "~ test" in output
        assert "type" in output

    def test_show_delete(self):
        cs = ChangeSet(
            asset_changes=[
                Change(action="delete", asset_id="old", before={"id": "old"})
            ]
        )
        plan = Plan(changeset=cs, environment="dev")
        output = plan.show()
        assert "- old" in output

    def test_show_mixed(self):
        cs = ChangeSet(
            asset_changes=[
                Change(action="create", asset_id="new"),
                Change(action="update", asset_id="mod", field_changes=[]),
                Change(action="delete", asset_id="old"),
            ]
        )
        plan = Plan(changeset=cs, environment="dev")
        output = plan.show()
        assert "3 change(s)" in output

    def test_show_update_with_no_field_changes(self):
        cs = ChangeSet(
            asset_changes=[
                Change(action="update", asset_id="test", field_changes=[]),
            ]
        )
        plan = Plan(changeset=cs, environment="dev")
        output = plan.show()
        assert "~ test" in output
        assert "1 to update" in output

    def test_repr(self):
        plan = Plan(environment="dev")
        assert "dev" in repr(plan)
        assert "changes=0" in repr(plan)

    def test_has_changes_with_dependency_changes_only(self):
        cs = ChangeSet(
            dependency_changes=[
                Change(action="create", asset_id="dep_a_b"),
            ]
        )
        plan = Plan(changeset=cs, environment="dev")
        assert plan.has_changes

    def test_change_id_helpers(self):
        cs = ChangeSet(
            asset_changes=[
                Change(action="create", asset_id="new"),
                Change(action="update", asset_id="mod"),
                Change(action="delete", asset_id="old"),
            ]
        )
        plan = Plan(changeset=cs, environment="dev")
        assert plan.changed_ids == {"new", "mod", "old"}
        assert plan.created_ids == {"new"}
        assert plan.updated_ids == {"mod"}
        assert plan.deleted_ids == {"old"}
