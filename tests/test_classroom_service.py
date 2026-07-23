from __future__ import annotations

from dataclasses import replace

import pytest

from gamgui.core.classroom.index import CourseIndex
from gamgui.core.classroom.manifests import RosterManifestStore
from gamgui.core.classroom.service import (
    ROSTER_CHANGE_CAP,
    ClassroomService,
    ClassroomValidationError,
)
from tests.classroom_fakes import FakeClassroomConnector, member

pytestmark = pytest.mark.asyncio


@pytest.fixture
def classroom(tmp_path):
    connector = FakeClassroomConnector()
    index = CourseIndex(tmp_path / "courses.db")
    manifests = RosterManifestStore(tmp_path / "operations.db")
    service = ClassroomService(connector, "example.com", index, manifests)
    return service, connector, index, manifests


async def test_refresh_builds_index_and_search_does_not_call_gam(classroom):
    service, connector, _, _ = classroom
    assert await service.refresh_index() == 1
    assert connector.calls == [("refresh_course_index",)]
    connector.calls.clear()

    page = await service.search("English")
    assert page.total == 1
    assert page.items[0].id == "123"
    assert connector.calls == []


async def test_create_is_provisioned_and_requires_active_directory_owner(classroom):
    service, connector, _, _ = classroom
    result = await service.create_course(
        name="Geometry",
        owner_email="Teacher@Example.com",
        alias="Section_456",
    )
    assert result.ok
    call = next(call for call in connector.calls if call[0] == "create_course")
    assert call[1]["state"] == "PROVISIONED"
    assert call[1]["owner_email"] == "teacher@example.com"

    with pytest.raises(ClassroomValidationError, match="suspended"):
        await service.create_course(
            name="Geometry", owner_email="suspended@example.com"
        )


async def test_metadata_and_state_changes_re_read_and_patch_index(classroom):
    service, connector, index, _ = classroom
    await service.refresh_index()
    result, current = await service.update_metadata(
        "123",
        name="English I",
        section="S2",
        room="301",
        description_heading="Start",
        description="Updated",
    )
    assert result.ok and current.name == "English I"
    assert index.search("example.com", "English I").total == 1

    archived, current = await service.transition_state("123", "ARCHIVED")
    assert archived.ok and current.course_state == "ARCHIVED"
    assert index.search("example.com", state="ARCHIVED").total == 1

    reactivated, current = await service.transition_state("123", "ACTIVE")
    assert reactivated.ok and current.course_state == "ACTIVE"

    with pytest.raises(ClassroomValidationError, match="cannot transition"):
        await service.transition_state("123", "PROVISIONED")


async def test_owner_transfer_requires_active_user_and_verifies_result(classroom):
    service, connector, _, _ = classroom
    preview = await service.prepare_owner_transfer("123", "newowner@example.com")
    assert preview.target_email == "newowner@example.com"
    assert preview.target_owner_id == "id-newowner@example.com"
    before_transfer = len(connector.calls)
    result, current = await service.transfer_owner("123", "newowner@example.com")
    assert result.ok
    assert current.owner_id == "id-newowner@example.com"
    assert [
        call
        for call in connector.calls[before_transfer:]
        if call[0] == "get_course"
    ] == [
        ("get_course", "123", False, False, False),
        ("get_course", "123", False, False, False),
    ]

    transfer_calls = len(
        [call for call in connector.calls if call[0] == "transfer_course_owner"]
    )
    with pytest.raises(ClassroomValidationError, match="already owns"):
        await service.prepare_owner_transfer("123", "newowner@example.com")
    assert len(
        [call for call in connector.calls if call[0] == "transfer_course_owner"]
    ) == transfer_calls


async def test_owner_transfer_fails_closed_when_live_owner_is_missing(classroom):
    service, connector, _, _ = classroom
    original = connector.transfer_course_owner

    async def transfer_without_verifiable_owner(course_id, target_email):
        result = await original(course_id, target_email)
        connector.courses[course_id] = replace(
            connector.courses[course_id],
            owner_email="",
            owner_id="",
        )
        return result

    connector.transfer_course_owner = transfer_without_verifiable_owner
    result, current = await service.transfer_owner("123", "newowner@example.com")
    assert not result.ok
    assert current.owner_email == ""
    assert "did not show the new owner" in result.detail
    assert connector.calls[-1] == ("get_course", "123", True, False, False)


async def test_owner_transfer_repairs_orphaned_owner_without_owner_email_enrichment(
    classroom,
):
    service, connector, _, _ = classroom
    connector.courses["123"] = replace(
        connector.courses["123"], owner_email="", owner_id="deleted-owner"
    )
    original = connector.get_course

    async def reject_orphan_owner_enrichment(
        course_id,
        *,
        include_owner_email=False,
        include_aliases=False,
        best_effort_enrichment=False,
    ):
        if include_owner_email:
            raise RuntimeError("owner lookup not found")
        return await original(
            course_id,
            include_owner_email=include_owner_email,
            include_aliases=include_aliases,
            best_effort_enrichment=best_effort_enrichment,
        )

    connector.get_course = reject_orphan_owner_enrichment

    preview = await service.prepare_owner_transfer("123", "newowner@example.com")
    result, current = await service.transfer_owner("123", "newowner@example.com")

    assert preview.target_email == "newowner@example.com"
    assert result.ok
    assert current.owner_id == "id-newowner@example.com"
    assert not any(
        call[0] == "get_course" and call[2] for call in connector.calls
    )


async def test_owner_teacher_cannot_be_removed(classroom):
    service, connector, _, _ = classroom
    with pytest.raises(ClassroomValidationError, match="owner cannot be removed"):
        await service.remove_member("123", "teachers", "teacher@example.com")
    assert not any(call[0] == "remove_course_participant" for call in connector.calls)


async def test_owner_removal_guard_falls_back_to_owner_id(classroom):
    service, connector, _, _ = classroom
    connector.courses["123"] = replace(
        connector.courses["123"], owner_email="", owner_id="owner-1"
    )
    with pytest.raises(ClassroomValidationError, match="owner cannot be removed"):
        await service.remove_member("123", "teachers", "teacher@example.com")
    assert not any(call[0] == "remove_course_participant" for call in connector.calls)


@pytest.mark.parametrize(
    "tampered_value",
    [
        "owner-1",
        "not-a-member@example.com",
        "$(touch nope)",
    ],
)
async def test_remove_member_requires_exact_live_roster_email(
    classroom,
    tampered_value,
):
    service, connector, _, _ = classroom
    with pytest.raises(
        ClassroomValidationError,
        match="current roster member|not a current member",
    ):
        await service.remove_member(
            "123",
            "teachers",
            tampered_value,
        )
    assert not any(
        call[0] == "remove_course_participant"
        for call in connector.calls
    )


async def test_exact_roster_plan_persists_diff_and_enforces_cap(classroom):
    service, connector, _, _ = classroom
    manifest = await service.plan_roster(
        "123",
        "students",
        ["student2@example.com", "student3@example.com"],
    )
    assert manifest.adds == ("student3@example.com",)
    assert manifest.removes == ("student1@example.com",)
    assert manifest.unchanged == ("student2@example.com",)

    connector.rosters[("123", "students")] = [
        member(f"student{i}@example.com", "students")
        for i in range(ROSTER_CHANGE_CAP + 1)
    ]
    with pytest.raises(ClassroomValidationError, match="safety cap"):
        await service.plan_roster("123", "students", [])


async def test_teacher_roster_requires_owner_in_desired_state(classroom):
    service, _, _, _ = classroom
    with pytest.raises(ClassroomValidationError, match="Include the course owner"):
        await service.plan_roster("123", "teachers", ["assistant@example.com"])


async def test_manifest_apply_rechecks_live_roster_and_persists_results(classroom):
    service, connector, _, manifests = classroom
    manifest = await service.plan_roster(
        "123",
        "students",
        ["student2@example.com", "student3@example.com"],
    )
    result = await service.apply_manifest(manifest.id)
    assert result.status == "completed"
    assert result.done_count == 2
    assert {item.email for item in connector.rosters[("123", "students")]} == {
        "student2@example.com",
        "student3@example.com",
    }
    assert manifests.get(manifest.id).status == "completed"


async def test_manifest_refuses_stale_preview_and_requires_replan(classroom):
    service, connector, _, manifests = classroom
    manifest = await service.plan_roster(
        "123",
        "students",
        ["student2@example.com", "student3@example.com"],
    )
    connector.rosters[("123", "students")].append(
        member("latechange@example.com", "students")
    )
    with pytest.raises(ClassroomValidationError, match="live roster changed"):
        await service.apply_manifest(manifest.id)
    assert manifests.get(manifest.id).status == "stale"

    fresh = await service.replan_manifest(manifest.id)
    assert fresh.id != manifest.id
    assert "latechange@example.com" in fresh.removes


async def test_partial_apply_records_residual_drift(classroom):
    service, connector, _, _ = classroom
    connector.fail_for.add("student1@example.com")
    manifest = await service.plan_roster(
        "123", "students", ["student2@example.com"]
    )
    result = await service.apply_manifest(manifest.id)
    assert result.status == "partial"
    assert result.failed_count == 1
    assert result.residual == ("remove:student1@example.com",)


async def test_failed_addition_prevents_all_roster_removals(classroom):
    service, connector, _, _ = classroom
    connector.fail_for.add("student3@example.com")
    manifest = await service.plan_roster(
        "123",
        "students",
        ["student2@example.com", "student3@example.com"],
    )
    result = await service.apply_manifest(manifest.id)
    assert result.status == "partial"
    assert not any(
        call[0] == "remove_course_participant"
        for call in connector.calls
    )
    skipped = next(
        target
        for target in result.targets
        if target.action == "remove"
    )
    assert skipped.status == "failed"
    assert "No removals were attempted" in skipped.detail
    assert {
        member.email
        for member in connector.rosters[("123", "students")]
    } == {"student1@example.com", "student2@example.com"}


async def test_post_apply_verification_failure_never_leaves_manifest_running(classroom):
    service, connector, _, manifests = classroom
    manifest = await service.plan_roster(
        "123",
        "students",
        ["student2@example.com", "student3@example.com"],
    )
    original = connector.list_course_participants
    calls = 0

    async def fail_verification(course_id, role):
        nonlocal calls
        calls += 1
        if calls == 1:
            return await original(course_id, role)
        raise RuntimeError("verification unavailable")

    connector.list_course_participants = fail_verification
    result = await service.apply_manifest(manifest.id)
    assert result.status == "failed"
    assert "Post-operation verification failed" in result.error
    assert manifests.get(manifest.id).status == "failed"


async def test_suspended_and_archived_courses_block_roster_changes(classroom):
    service, connector, _, _ = classroom
    connector.courses["123"] = replace(
        connector.courses["123"], course_state="ARCHIVED"
    )
    with pytest.raises(ClassroomValidationError, match="Activate the course"):
        await service.add_member("123", "students", "new@example.com")
