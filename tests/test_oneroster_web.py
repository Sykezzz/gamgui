from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import hashlib
from io import BytesIO
import json
from pathlib import Path
import re
import threading
import time
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from gamgui.web.routes import oneroster as oneroster_routes
from gamgui.web.routes.oneroster import (
    _BoundedUploadStream,
    _planning_confirmation_hash,
    _planning_record,
    router,
)


TEMPLATES = Path(__file__).parents[1] / "gamgui" / "web" / "templates"


class FakeComponentManager:
    def __init__(self, *, installed=True, enabled=True) -> None:
        self.installed = installed
        self.enabled = enabled

    def status(self, component_id: str):
        return {
            "state": (
                "enabled"
                if self.enabled
                else "installed_disabled"
                if self.installed
                else "not_installed"
            ),
            "installed": self.installed,
            "enabled": self.enabled,
            "first_run_pending": not self.installed,
        }


class FakeOneRosterService:
    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.plan_held = False
        self.gate_error = False
        self.execution_fail_after_first = False
        self.snapshots = {
            "import-1": {
                "id": "import-1",
                "filename": "OneRoster.zip",
                "state": "validated",
                "package_mode": "bulk",
                "imported_at": "2026-07-25T10:00:00-05:00",
                "expires_at": "2026-08-24T10:00:00-05:00",
                "counts": {"courses": 2, "students": 100},
                "issue_count": 1,
                "blocking_issue_count": 0,
                "selected_session_id": "",
                "ready_for_apply": False,
            }
        }
        self.profile = {
            "version": 1,
            "configured": False,
            "limited_import": False,
            "limits": {},
            "blackouts": [],
        }
        self.gate = {
            "state": "CLOSED",
            "timezone": "America/Chicago",
            "manifest_id": "",
            "manifest_hash": "",
            "release_at": "",
            "hold_code": "",
            "hold_detail": "",
        }
        self.manifests: dict[str, SimpleNamespace] = {}

    def history(self, limit=50):
        self.calls.append(("history", limit))
        return tuple(list(self.snapshots.values())[:limit])

    def get_threshold_profile(self):
        self.calls.append(("thresholds",))
        return dict(self.profile)

    def save_threshold_profile(self, profile):
        self.calls.append(("save_thresholds", profile))
        self.profile = dict(profile)
        self.profile["configured"] = True
        return dict(self.profile)

    def get_gate(self):
        self.calls.append(("gate",))
        return dict(self.gate)

    def upload(self, source, filename="upload.zip"):
        self.calls.append(("upload", filename))
        assert source.read(2) == b"PK"
        snapshot = dict(self.snapshots["import-1"])
        snapshot["filename"] = filename
        self.snapshots["import-1"] = snapshot
        return snapshot

    def get_import(self, import_id):
        self.calls.append(("get_import", import_id))
        return dict(self.snapshots[import_id])

    def preview(self, import_id, kind, query="", cursor=None, limit=50):
        self.calls.append(("preview", import_id, kind, query, cursor, limit))
        if kind == "sessions":
            return {
                "items": [
                    {
                        "sourced_id": "term-2026",
                        "title": "2026-27",
                        "school_year": "2026",
                    }
                ],
                "next_cursor": None,
                "total": 1,
                "limit": limit,
            }
        rows = [
            {
                "id": f"course-{number}",
                "name": f"English {number}",
                "alias": f"Section_{number}",
                "owner": "teacher@example.com",
            }
            for number in range(limit)
        ]
        return {
            "items": rows,
            "next_cursor": "opaque-next" if cursor is None else None,
            "total": 500,
            "limit": limit,
        }

    def select_session(self, import_id, session_id):
        self.calls.append(("select_session", import_id, session_id))
        snapshot = dict(self.snapshots[import_id])
        snapshot.update(
            selected_session_id=session_id,
            state="ready",
            ready_for_apply=True,
        )
        self.snapshots[import_id] = snapshot
        return snapshot

    def write_export(self, import_id, kind, stream):
        self.calls.append(("write_export", import_id, kind))
        stream.write(b"alias,name\nSection_1,English 1\n")
        return 1

    async def build_live_plan(self, connector, import_id, limited_import=False):
        self.calls.append(("build_live_plan", connector, import_id, limited_import))
        action = SimpleNamespace(
            id="action-1",
            kind="course_create",
            subject="Section_1",
            target="teacher@example.com",
            before="",
            after="English 1",
            status="pending",
            detail="",
        )
        breach = SimpleNamespace(
            action="course_create",
            actual_count=1,
            baseline_count=0,
            actual_percent=None,
            max_count=0,
            max_percent=None,
            reason="count 1 exceeds 0",
        )
        evaluation = SimpleNamespace(
            held=self.plan_held and not limited_import,
            limited_import=limited_import,
            blackout=False,
            evaluated_at=1.0,
            counts={"course_create": 1},
            baselines={"course_create": 0},
            breaches=(breach,) if self.plan_held and not limited_import else (),
            profile_hash="f" * 64,
        )
        return SimpleNamespace(
            import_id=import_id,
            source_hash="a" * 64,
            config_hash="b" * 64,
            live_hash="c" * 64,
            actions=(action,),
            archive_actions=(
                SimpleNamespace(
                    **{
                        **action.__dict__,
                        "id": "archive-1",
                        "kind": "course_archive",
                    }
                ),
            )
            if not limited_import
            else (),
            ownership_actions=(
                SimpleNamespace(
                    **{
                        **action.__dict__,
                        "id": "owner-1",
                        "kind": "owner_transfer",
                    }
                ),
            )
            if not limited_import
            else (),
            issues=(),
            limited_import=limited_import,
            threshold_evaluation=evaluation,
        )

    def persist_live_plan(self, planning):
        self.calls.append(("persist_live_plan", planning.import_id))

        def manifest(identifier, kind, actions):
            value = SimpleNamespace(
                id=identifier,
                domain="example.com",
                import_id=planning.import_id,
                source_hash=planning.source_hash,
                config_hash=planning.config_hash,
                live_hash=planning.live_hash,
                manifest_hash=identifier[0] * 64,
                status="planned",
                created_at=1.0,
                actions=tuple(actions),
                threshold_evaluation_hash="d" * 64,
                error="",
                plan_kind=kind,
                confirmed_at=0.0,
            )
            self.manifests[identifier] = value
            return value

        ordinary = manifest("1" * 32, "ordinary", planning.actions)
        archive = (
            manifest("2" * 32, "archive", planning.archive_actions)
            if planning.archive_actions
            else None
        )
        ownership = (
            manifest("3" * 32, "ownership", planning.ownership_actions)
            if planning.ownership_actions
            else None
        )
        return SimpleNamespace(
            ordinary=ordinary,
            archive=archive,
            ownership=ownership,
            issues=(),
        )

    def record_override(
        self,
        import_id,
        evaluation,
        reason,
        typed_import_id,
    ):
        self.calls.append(
            ("record_override", import_id, reason, typed_import_id)
        )
        if typed_import_id != import_id:
            raise FakeOneRosterError(
                "OR-OVERRIDE-CONFIRMATION",
                "The typed import ID does not match this held import.",
            )
        return {"import_id": import_id}

    def get_manifest(self, manifest_id):
        self.calls.append(("get_manifest", manifest_id))
        return self.manifests[manifest_id]

    async def execute_manifest(
        self,
        connector,
        manifest_id,
        typed_import_id="",
    ):
        self.calls.append(
            ("execute_manifest", connector, manifest_id, typed_import_id)
        )
        manifest = self.manifests[manifest_id]
        if typed_import_id != manifest.import_id:
            raise FakeOneRosterError(
                "OR-CONFIRMATION-MISMATCH",
                "The typed import ID does not match.",
            )
        manifest.confirmed_at = 2.0
        manifest.status = "running"
        for index, action in enumerate(manifest.actions):
            action.status = "applied"
            action.detail = "Verified against live Classroom state."
            if self.execution_fail_after_first and index == 0:
                manifest.status = "interrupted"
                manifest.error = "OR-EXECUTION-FAILED"
                raise RuntimeError("controlled failure after an applied batch")
        manifest.status = "completed"
        return SimpleNamespace(
            manifest=manifest,
            applied=len(manifest.actions),
            failed=0,
            skipped=0,
            awaiting_students=False,
        )

    def close_gate(self):
        self.calls.append(("close_gate",))
        self.gate["state"] = "CLOSED"
        return dict(self.gate)

    def arm_gate(self, manifest_id, manifest_hash, release_at):
        self.calls.append(("arm_gate", manifest_id, manifest_hash, release_at))
        self.gate.update(
            state="ARMED",
            manifest_id=manifest_id,
            manifest_hash=manifest_hash,
            release_at=release_at,
        )
        return dict(self.gate)

    async def revalidate_scheduled_gate(
        self,
        connector,
        manifest_id,
        now=None,
        manual=False,
    ):
        self.calls.append(("revalidate_gate", connector, manifest_id, manual))
        if self.gate_error:
            raise FakeOneRosterError("OR-GATE-DRIFT", "The manifest changed.")
        self.gate["state"] = "OPEN"
        return dict(self.gate)


class FakeOneRosterError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


def _client(*, installed=True, enabled=True, service=True):
    component = FakeComponentManager(installed=installed, enabled=enabled)
    engine = FakeOneRosterService() if service else None
    state = SimpleNamespace(
        component_manager=component,
        oneroster_service=engine,
        audit_domain="example.com",
        connector=object(),
        oneroster_manifest_tasks={},
        oneroster_manifest_errors={},
    )
    app = FastAPI()
    app.state.gamgui = state
    app.include_router(router)
    return TestClient(app), engine


def test_core_deep_link_explains_install_without_service_call():
    client, _ = _client(installed=False, enabled=False, service=False)

    response = client.get("/classroom/imports")

    assert response.status_code == 200
    assert "Install OneRoster Classroom" in response.text
    assert "CMP-NOT-INSTALLED" in response.text
    assert "Core Classroom" in response.text


def test_disabled_and_degraded_states_are_isolated():
    disabled, _ = _client(installed=True, enabled=False)
    response = disabled.get("/classroom/imports")
    assert "CMP-DISABLED" in response.text

    degraded, _ = _client(installed=True, enabled=True, service=False)
    response = degraded.get("/classroom/imports")
    assert "CMP-INCOMPATIBLE" in response.text
    assert "Core Classroom tools remain available" in response.text


def test_enabled_component_without_domain_uses_auth_required_code():
    component = FakeComponentManager(installed=True, enabled=True)
    state = SimpleNamespace(
        component_manager=component,
        oneroster_service=None,
        audit_domain="",
    )
    app = FastAPI()
    app.state.gamgui = state
    app.include_router(router)

    response = TestClient(app).get("/classroom/imports")

    assert response.status_code == 200
    assert "CMP-AUTH-REQUIRED" in response.text
    assert "Connect a Workspace domain" in response.text


def test_dashboard_reads_only_local_import_configuration():
    client, service = _client()
    response = client.get("/classroom/imports")

    assert response.status_code == 200
    assert "Import Studio" in response.text
    assert "OneRoster.zip" in response.text
    assert ("history", 50) in service.calls
    assert ("thresholds",) in service.calls
    assert ("gate",) in service.calls
    assert not any(call[0] in {"gam", "google", "connector"} for call in service.calls)


def test_upload_rejects_non_zip_before_service_and_accepts_zip_stream():
    client, service = _client()
    rejected = client.post(
        "/classroom/imports/upload",
        files={"package": ("roster.csv", b"users", "text/csv")},
    )
    assert "OR-ZIP-INVALID" in rejected.text
    assert not any(call[0] == "upload" for call in service.calls)

    accepted = client.post(
        "/classroom/imports/upload",
        files={"package": ("district.zip", b"PKfixture", "application/zip")},
    )
    assert accepted.status_code == 200
    assert "district.zip" in accepted.text
    assert "Import ID" in accepted.text
    assert ("upload", "district.zip") in service.calls


def test_bounded_upload_stream_rejects_bytes_past_server_cap():
    stream = _BoundedUploadStream(BytesIO(b"12345"), 4)

    assert stream.read(4) == b"1234"
    try:
        stream.read(4)
    except ValueError as exc:
        assert "250 MB upload limit" in str(exc)
    else:
        raise AssertionError("expected the bounded stream to reject trailing bytes")


def test_validate_uses_eagerly_validated_snapshot_without_remote_call():
    client, service = _client()
    response = client.post("/classroom/imports/import/import-1/validate")

    assert response.status_code == 200
    assert "Local validation completed" in response.text
    assert ("get_import", "import-1") in service.calls


def test_preview_is_server_bounded_and_cursor_is_opaque():
    client, service = _client()
    response = client.get(
        "/classroom/imports/import/import-1/preview",
        params={"kind": "courses", "limit": 500, "q": "English"},
    )

    assert response.status_code == 200
    assert response.text.count("Section_") == 50
    assert "Next 50" in response.text
    assert "opaque-next" in response.text
    assert len(response.content) < 100_000
    assert (
        "preview",
        "import-1",
        "courses",
        "English",
        None,
        50,
    ) in service.calls


def test_preview_response_stays_under_one_hundred_kilobytes_for_hostile_cells():
    client, service = _client()

    def hostile_preview(
        import_id,
        kind,
        query="",
        cursor=None,
        limit=50,
    ):
        return {
            "items": [
                {
                    f"column_{column}": "&" * 10_000
                    for column in range(8)
                }
                for _ in range(limit)
            ],
            "next_cursor": "opaque-next",
            "total": 500,
            "limit": limit,
        }

    service.preview = hostile_preview
    response = client.get(
        "/classroom/imports/import/import-1/preview",
        params={"kind": "courses", "limit": 50},
    )

    assert response.status_code == 200
    assert len(response.content) <= 100_000
    assert "&amp;" in response.text
    assert "&" * 1_000 not in response.text


def test_preview_rejects_unallowlisted_kind_before_service():
    client, service = _client()
    response = client.get(
        "/classroom/imports/import/import-1/preview",
        params={"kind": "raw_html"},
    )

    assert "OR-PREVIEW-INVALID" in response.text
    assert not any(call[0] == "preview" for call in service.calls)


def test_academic_session_selection_is_local_and_rebuilds_import():
    client, service = _client()
    preview = client.get(
        "/classroom/imports/import/import-1/preview",
        params={"kind": "sessions"},
    )
    assert preview.status_code == 200
    assert "Use this session" in preview.text
    assert "term-2026" in preview.text

    selected = client.post(
        "/classroom/imports/import/import-1/session",
        data={"session_id": "term-2026"},
    )
    assert selected.status_code == 200
    assert "Academic session selected" in selected.text
    assert "term-2026" in selected.text
    assert ("select_session", "import-1", "term-2026") in service.calls
    assert not any(call[0] == "build_live_plan" for call in service.calls)


def test_gam_export_is_allowlisted_streamed_and_not_cached():
    client, service = _client()
    response = client.get(
        "/classroom/imports/import/import-1/export/courses"
    )

    assert response.status_code == 200
    assert response.content == b"alias,name\nSection_1,English 1\n"
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-oneroster-row-count"] == "1"
    assert "attachment" in response.headers["content-disposition"]
    assert ("write_export", "import-1", "courses") in service.calls

    rejected = client.get(
        "/classroom/imports/import/import-1/export/raw"
    )
    assert "OR-EXPORT-INVALID" in rejected.text


def test_live_plan_requires_explicit_connector_and_does_not_run_on_open():
    client, service = _client()
    client.app.state.gamgui.connector = None

    response = client.post(
        "/classroom/imports/import/import-1/plan",
        data={"mode": "normal"},
    )

    assert "CMP-AUTH-REQUIRED" in response.text
    assert not any(call[0] == "build_live_plan" for call in service.calls)


def test_threshold_hold_supports_drift_checked_override_and_limited_plan():
    client, service = _client()
    service.plan_held = True
    held = client.post(
        "/classroom/imports/import/import-1/plan",
        data={"mode": "normal", "pilot_evidence": "Pilot evidence reviewed."},
    )

    assert held.status_code == 200
    assert "Threshold hold" in held.text
    assert "Override this exact held plan" in held.text
    assert not any(call[0] == "persist_live_plan" for call in service.calls)
    match = re.search(
        r'name="expected_plan_hash" value="([0-9a-f]{64})"',
        held.text,
    )
    assert match is not None

    overridden = client.post(
        "/classroom/imports/import/import-1/override",
        data={
            "typed_import_id": "import-1",
            "reason": "Approved after enrollment review",
            "expected_plan_hash": match.group(1),
            "pilot_evidence": "Pilot evidence reviewed.",
        },
    )
    assert overridden.status_code == 200
    assert "Threshold override recorded" in overridden.text
    assert "Ordinary approval" in overridden.text
    assert "Archive approval" in overridden.text
    assert "Ownership approval" in overridden.text
    assert any(call[0] == "record_override" for call in service.calls)

    limited = client.post(
        "/classroom/imports/import/import-1/plan",
        data={"mode": "limited", "pilot_evidence": "Pilot evidence reviewed."},
    )
    assert limited.status_code == 200
    assert "Immutable additions-only manifests created" in limited.text
    assert "Archive approval" not in limited.text
    assert "Ownership approval" not in limited.text


def test_manifest_execution_requires_typed_import_id_and_polls_local_state():
    client, service = _client()
    planned = client.post(
        "/classroom/imports/import/import-1/plan",
        data={"mode": "normal", "pilot_evidence": "Pilot evidence reviewed."},
    )
    assert planned.status_code == 200
    manifest_id = "1" * 32

    rejected = client.post(
        f"/classroom/imports/manifest/{manifest_id}/execute",
        data={"typed_import_id": "wrong-import"},
    )
    assert "OR-CONFIRMATION-MISMATCH" in rejected.text
    assert not any(call[0] == "execute_manifest" for call in service.calls)

    queued = client.post(
        f"/classroom/imports/manifest/{manifest_id}/execute",
        data={"typed_import_id": "import-1"},
    )
    assert queued.status_code == 200
    assert "Execution was queued" in queued.text
    assert "hx-trigger=" in queued.text

    status = client.get(
        f"/classroom/imports/manifest/{manifest_id}/status"
    )
    assert status.status_code == 200
    assert "Completed" in status.text
    assert any(call[0] == "execute_manifest" for call in service.calls)
    calls_before = len(service.calls)
    client.get(f"/classroom/imports/manifest/{manifest_id}/status")
    assert not any(
        call[0] in {"build_live_plan", "execute_manifest"}
        for call in service.calls[calls_before:]
    )


def test_late_execution_failure_warns_of_partial_apply_and_shows_results():
    client, service = _client()
    client.post(
        "/classroom/imports/import/import-1/plan",
        data={"mode": "normal", "pilot_evidence": "Pilot evidence reviewed."},
    )
    manifest_id = "1" * 32
    service.execution_fail_after_first = True

    queued = client.post(
        f"/classroom/imports/manifest/{manifest_id}/execute",
        data={"typed_import_id": "import-1"},
    )
    assert queued.status_code == 200

    status = client.get(f"/classroom/imports/manifest/{manifest_id}/status")

    assert status.status_code == 200
    assert "may already have been applied" in status.text
    assert "persisted per-action results" in status.text
    assert "create and confirm a new plan" in status.text
    assert "No Classroom changes were made" not in status.text
    assert ">Applied<" in status.text
    assert "Verified against live Classroom state" in status.text
    assert "controlled failure" not in status.text


def test_manifest_actions_are_bounded_to_fifty_with_local_paging():
    client, service = _client()
    planning = client.post(
        "/classroom/imports/import/import-1/plan",
        data={"mode": "normal", "pilot_evidence": "Pilot evidence reviewed."},
    )
    assert planning.status_code == 200
    manifest_id = "1" * 32
    manifest = service.manifests[manifest_id]
    manifest.actions = tuple(
        SimpleNamespace(
            id=f"action-{number}",
            kind="course_create",
            subject=f"Section_{number}",
            target=f"teacher{number}@example.com",
            before="",
            after=f"Course {number}",
            status="pending",
            detail="",
        )
        for number in range(75)
    )

    first = client.get(f"/classroom/imports/manifest/{manifest_id}")
    assert first.status_code == 200
    assert first.text.count(">Course Create<") == 50
    assert "Next" in first.text
    assert len(first.content) < 100_000

    second = client.get(
        f"/classroom/imports/manifest/{manifest_id}",
        params={"offset": 50},
    )
    assert second.text.count(">Course Create<") == 25
    assert "Previous" in second.text
    assert len(second.content) < 100_000


def test_thresholds_validate_numbers_and_save_profile():
    client, service = _client()
    actions = (
        "course_create",
        "course_update",
        "course_archive",
        "teacher_add",
        "teacher_remove",
        "student_add",
        "student_remove",
        "owner_mismatch",
        "record_rejected",
    )
    invalid = client.post(
        "/classroom/imports/thresholds",
        data={"mode": "normal", "student_remove_percent": "101"},
    )
    assert "OR-THRESHOLD-INVALID" in invalid.text
    assert not any(call[0] == "save_thresholds" for call in service.calls)

    incomplete = client.post(
        "/classroom/imports/thresholds",
        data={"mode": "normal"},
    )
    assert "OR-THRESHOLD-INCOMPLETE" in incomplete.text
    assert not any(call[0] == "save_thresholds" for call in service.calls)

    threshold_data = {
        "mode": "limited",
        **{f"{action}_disabled": "true" for action in actions},
    }
    threshold_data.pop("student_remove_disabled")
    threshold_data.update(
        student_remove_count="200",
        student_remove_percent="20",
    )
    valid = client.post(
        "/classroom/imports/thresholds",
        data=threshold_data,
    )
    assert valid.status_code == 200
    assert "Threshold profile saved" in valid.text
    call = next(call for call in service.calls if call[0] == "save_thresholds")
    assert call[1]["limited_import"] is True
    assert call[1]["limits"]["student_remove"]["max_count"] == 200

    conflict = client.post(
        "/classroom/imports/thresholds",
        data={
            **{f"{action}_disabled": "true" for action in actions},
            "course_create_count": "10",
        },
    )
    assert "OR-THRESHOLD-INVALID" in conflict.text


def test_student_gate_requires_manifest_and_typed_open_confirmation():
    client, service = _client()
    manifest_hash = "a" * 64
    rejected = client.post(
        "/classroom/imports/gate",
        data={
                "target_state": "OPEN",
                "manifest_id": "manifest-1",
                "confirmation": "open",
            },
        )
    assert "OR-GATE-CONFIRMATION" in rejected.text
    assert not any(call[0] == "revalidate_gate" for call in service.calls)

    armed = client.post(
        "/classroom/imports/gate",
        data={
            "target_state": "ARMED",
            "manifest_id": "manifest-1",
            "manifest_hash": manifest_hash,
            "release_at": datetime.now(timezone.utc).isoformat(),
        },
    )
    assert "now ARMED" in armed.text
    assert any(call[0] == "arm_gate" for call in service.calls)

    opened = client.post(
        "/classroom/imports/gate",
        data={
            "target_state": "OPEN",
            "manifest_id": "manifest-1",
            "manifest_hash": "b" * 64,
            "current_manifest_hash": "c" * 64,
            "confirmation": "OPEN",
        },
    )
    assert "now OPEN" in opened.text
    assert any(
        call[0] == "revalidate_gate" and call[3] is True
        for call in service.calls
    )
    assert any(
        call[0] == "execute_manifest"
        and call[2] == "manifest-1"
        and call[3] == ""
        for call in service.calls
    )
    assert "current_manifest_hash" not in opened.text


def test_gate_surfaces_stable_engine_error_without_raw_exception():
    client, service = _client()
    service.gate_error = True
    response = client.post(
        "/classroom/imports/gate",
        data={
            "target_state": "OPEN",
            "manifest_id": "manifest-1",
            "confirmation": "OPEN",
        },
    )

    assert "OR-GATE-DRIFT" in response.text
    assert "The manifest changed" in response.text
    assert "Traceback" not in response.text


def test_history_is_bounded_to_fifty_rows():
    client, service = _client()
    for number in range(75):
        service.snapshots[f"import-{number + 2}"] = {
            **service.snapshots["import-1"],
            "id": f"import-{number + 2}",
            "filename": f"Roster-{number + 2}.zip",
        }

    response = client.get("/classroom/imports/history")

    assert response.status_code == 200
    assert response.text.count(">Open<") == 50
    assert "Roster-76.zip" not in response.text


def test_import_controls_have_semantics_live_regions_and_focus_styles():
    client, _ = _client()
    response = client.get("/classroom/imports")

    assert 'aria-live="polite"' in response.text
    assert "focus-visible:ring-2" in response.text
    assert 'aria-labelledby="oneroster-upload-heading"' in response.text
    assert 'enctype="multipart/form-data"' in response.text
    assert "current_manifest_hash" not in response.text


def test_all_oneroster_buttons_links_and_summaries_have_keyboard_focus_styles():
    for path in TEMPLATES.glob("*oneroster*.html"):
        text = path.read_text(encoding="utf-8")
        for tag in re.findall(
            r"<(?:button|a|summary)\b[^>]*>",
            text,
            flags=re.I | re.S,
        ):
            assert "focus-visible:" in tag, (
                f"{path.name}: missing visible keyboard focus in {tag}"
            )


def _planning_evaluation():
    return SimpleNamespace(
        held=False,
        limited_import=False,
        blackout=False,
        evaluated_at=123.0,
        counts={"course_create": 2},
        baselines={"course_create": 10},
        breaches=(),
        profile_hash="f" * 64,
    )


def test_planning_confirmation_hash_preserves_canonical_legacy_digest():
    actions = (
        SimpleNamespace(
            id="action-2",
            kind="student_add",
            subject="Section_1",
            target="student@example.com",
            before="",
            after="student",
            status="pending",
            detail="",
        ),
        SimpleNamespace(
            id="action-1",
            kind="course_create",
            subject="Section_1",
            target="teacher@example.com",
            before="",
            after="English 1",
            status="pending",
            detail="",
        ),
    )
    archive = (
        SimpleNamespace(
            id="archive-1",
            kind="course_archive",
            subject="Section_0",
            target="course-0",
            before="ACTIVE",
            after="ARCHIVED",
            status="pending",
            detail="",
        ),
    )
    evaluation = _planning_evaluation()
    planning = SimpleNamespace(
        import_id="import-1",
        source_hash="a" * 64,
        config_hash="b" * 64,
        live_hash="c" * 64,
        limited_import=False,
        actions=actions,
        archive_actions=archive,
        ownership_actions=(),
        issues=(),
        threshold_evaluation=evaluation,
    )

    bounded_actions = [
        oneroster_routes._action_record(action)
        for action in (*actions, *archive)
    ]
    evaluation_record = oneroster_routes._evaluation_record(evaluation)
    legacy_payload = {
        "import_id": planning.import_id,
        "source_hash": planning.source_hash,
        "config_hash": planning.config_hash,
        "live_hash": planning.live_hash,
        "limited_import": planning.limited_import,
        "actions": [
            {
                key: item.get(key, "")
                for key in ("id", "kind", "subject", "target", "before", "after")
            }
            for item in bounded_actions
        ],
        "threshold": {
            key: evaluation_record.get(key)
            for key in (
                "held",
                "limited_import",
                "blackout",
                "counts",
                "baselines",
                "breaches",
                "profile_hash",
            )
        },
    }
    expected = hashlib.sha256(
        json.dumps(
            legacy_payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            default=str,
        ).encode("utf-8")
    ).hexdigest()

    assert _planning_confirmation_hash(planning) == expected
    assert _planning_record(planning)["confirmation_hash"] == expected


class _OneShotActionStream:
    def __init__(self, count: int, kind: str, prefix: str) -> None:
        self.count = count
        self.kind = kind
        self.prefix = prefix
        self.iterations = 0
        self.yielded = 0

    def __iter__(self):
        if self.iterations:
            raise AssertionError("planning traversed an action group more than once")
        self.iterations += 1
        for number in range(self.count):
            self.yielded += 1
            yield SimpleNamespace(
                id=f"{self.prefix}-{number}",
                kind=self.kind,
                subject=f"Section_{number}",
                target=f"target-{number}@example.com",
                before="",
                after="ready",
                status="pending",
                detail="",
            )


class _OneShotIssueStream:
    def __init__(self, count: int) -> None:
        self.count = count
        self.iterations = 0

    def __iter__(self):
        if self.iterations:
            raise AssertionError("planning traversed issues more than once")
        self.iterations += 1
        for number in range(self.count):
            yield SimpleNamespace(
                code="OR-TEST",
                message=f"Issue {number}",
                blocking=False,
            )


def test_planning_record_streams_large_plan_and_retains_only_bounded_previews(
    monkeypatch,
):
    ordinary = _OneShotActionStream(12_000, "course_create", "ordinary")
    archive = _OneShotActionStream(8_000, "course_archive", "archive")
    ownership = _OneShotActionStream(6_000, "owner_transfer", "ownership")
    issues = _OneShotIssueStream(3_000)
    planning = SimpleNamespace(
        import_id="import-large",
        source_hash="a" * 64,
        config_hash="b" * 64,
        live_hash="c" * 64,
        limited_import=False,
        actions=ordinary,
        archive_actions=archive,
        ownership_actions=ownership,
        issues=issues,
        threshold_evaluation=_planning_evaluation(),
    )
    original_dumps = oneroster_routes.json.dumps

    def reject_full_plan_json(value, *args, **kwargs):
        assert not (
            isinstance(value, dict)
            and isinstance(value.get("actions"), list)
        ), "confirmation hashing buffered the full action plan"
        return original_dumps(value, *args, **kwargs)

    monkeypatch.setattr(oneroster_routes.json, "dumps", reject_full_plan_json)

    record = _planning_record(planning)

    assert ordinary.iterations == archive.iterations == ownership.iterations == 1
    assert (ordinary.yielded, archive.yielded, ownership.yielded) == (
        12_000,
        8_000,
        6_000,
    )
    assert issues.iterations == 1
    assert record["action_total"] == 12_000
    assert record["archive_total"] == 8_000
    assert record["ownership_total"] == 6_000
    assert record["issue_total"] == 3_000
    assert len(record["actions"]) == 50
    assert len(record["archive_actions"]) == 50
    assert len(record["ownership_actions"]) == 50
    assert len(record["issues"]) == 50
    assert sum(
        len(record[group])
        for group in ("actions", "archive_actions", "ownership_actions")
    ) == 150
    assert record["action_counts"] == {
        "course_archive": 8_000,
        "course_create": 12_000,
        "owner_transfer": 6_000,
    }
    assert re.fullmatch(r"[0-9a-f]{64}", record["confirmation_hash"])


def test_manifest_record_streams_large_action_set_and_retains_one_page():
    actions = _OneShotActionStream(26_000, "student_add", "manifest")
    manifest = SimpleNamespace(
        id="manifest-large",
        status="planned",
        plan_kind="ordinary",
        actions=actions,
        exclusions=(),
        threshold_evidence={},
        pilot_evidence={},
    )

    record = oneroster_routes._manifest_record(manifest)

    assert actions.iterations == 1
    assert actions.yielded == 26_000
    assert record["action_total"] == 26_000
    assert record["pending_count"] == 26_000
    assert record["action_counts"] == {"student_add": 26_000}
    assert len(record["actions"]) == 50


@pytest.mark.asyncio
async def test_district_plan_render_and_confirmation_hash_run_off_event_loop(
    monkeypatch: pytest.MonkeyPatch,
):
    planning = SimpleNamespace(
        import_id="import-offload",
        source_hash="a" * 64,
        config_hash="b" * 64,
        live_hash="c" * 64,
        limited_import=False,
        actions=(),
        archive_actions=(),
        ownership_actions=(),
        issues=(),
        threshold_evaluation=_planning_evaluation(),
    )
    original_record = oneroster_routes._planning_record
    original_hash = oneroster_routes._planning_confirmation_hash
    worker_threads: list[int] = []

    def slow_record(value):
        worker_threads.append(threading.get_ident())
        time.sleep(0.05)
        return original_record(value)

    def slow_hash(value):
        worker_threads.append(threading.get_ident())
        time.sleep(0.05)
        return original_hash(value)

    monkeypatch.setattr(oneroster_routes, "_planning_record", slow_record)
    monkeypatch.setattr(
        oneroster_routes,
        "_planning_confirmation_hash",
        slow_hash,
    )
    monkeypatch.setattr(
        oneroster_routes.TEMPLATES,
        "TemplateResponse",
        lambda request, template, context: context,
    )
    main_thread = threading.get_ident()
    render_task = asyncio.create_task(
        oneroster_routes._plan_response(object(), planning)
    )
    hash_task = asyncio.create_task(
        oneroster_routes._planning_confirmation_hash_async(planning)
    )
    heartbeat = 0
    while not render_task.done() or not hash_task.done():
        heartbeat += 1
        await asyncio.sleep(0.005)

    context = await render_task
    digest = await hash_task
    assert context["planning"]["import_id"] == "import-offload"
    assert len(digest) == 64
    assert worker_threads and all(item != main_thread for item in worker_threads)
    assert heartbeat >= 5
