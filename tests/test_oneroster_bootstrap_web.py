from __future__ import annotations

import asyncio

from tests.test_oneroster_web import _client
from gamgui.components.oneroster import OneRosterError
from gamgui.web.routes.oneroster import _execution_error


MANIFEST_ID = "1" * 32


def test_active_job_error_confirms_bootstrap_did_not_submit_gam_phase():
    code, message = _execution_error(
        OneRosterError("OR-ACTIVE-JOB", "another job is active")
    )

    assert code == "CMP-ACTIVE-JOB"
    assert "did not start" in message
    assert "No additions-first GAM phase was submitted" in message
    assert "changes may already have been applied" not in message


class _MissingReportStore:
    def __init__(self, count: int = 0) -> None:
        self.count = count
        self.calls: list[tuple] = []

    def bootstrap_missing_report_count(self, manifest_id: str) -> int:
        self.calls.append(("count", manifest_id))
        return self.count

    def write_bootstrap_missing_report(self, manifest_id: str, stream) -> int:
        self.calls.append(("write", manifest_id))
        stream.write(b"action_id,kind,reason,cycle\n")
        for number in range(self.count):
            stream.write(
                f"action-{number},student_add,not visible,{number + 1}\n".encode()
            )
        return self.count


def _paused_limited_manifest():
    client, service = _client()
    response = client.post(
        "/classroom/imports/import/import-1/plan",
        data={"mode": "limited", "pilot_evidence": "Pilot evidence reviewed."},
    )
    assert response.status_code == 200
    manifest = service.manifests[MANIFEST_ID]
    manifest.plan_kind = "limited"
    manifest.status = "paused"
    return client, service, manifest


def test_bootstrap_form_is_only_shown_for_paused_limited_additive_manifest():
    client, _, manifest = _paused_limited_manifest()

    eligible = client.get(f"/classroom/imports/manifest/{MANIFEST_ID}")
    assert "Authorize additions-first bootstrap" in eligible.text
    assert 'name="import_id_ack"' in eligible.text
    assert 'name="deferred_verification_ack"' in eligible.text
    assert "course creation, student additions, course activation" in eligible.text
    assert "Teachers are submitted last" in eligible.text
    assert "course activation, student additions" not in eligible.text
    assert "verification before teacher or student work" not in eligible.text

    manifest.status = "planned"
    assert "Authorize additions-first bootstrap" not in client.get(
        f"/classroom/imports/manifest/{MANIFEST_ID}"
    ).text

    manifest.status = "paused"
    manifest.plan_kind = "ordinary"
    assert "Authorize additions-first bootstrap" not in client.get(
        f"/classroom/imports/manifest/{MANIFEST_ID}"
    ).text

    manifest.plan_kind = "limited"
    manifest.actions[0].kind = "course_archive"
    assert "Authorize additions-first bootstrap" not in client.get(
        f"/classroom/imports/manifest/{MANIFEST_ID}"
    ).text


def test_bootstrap_form_remains_available_when_every_action_is_submitted():
    client, _, manifest = _paused_limited_manifest()
    for action in manifest.actions:
        action.status = "submitted"

    response = client.get(f"/classroom/imports/manifest/{MANIFEST_ID}")

    assert "Authorize additions-first bootstrap" in response.text


def test_bootstrap_requires_both_acknowledgements_and_dispatches_in_background():
    client, service, manifest = _paused_limited_manifest()

    async def execute_additions_first_bootstrap(
        connector,
        manifest_id,
        *,
        import_id_ack,
        deferred_verification_ack,
    ):
        service.calls.append(
            (
                "execute_additions_first_bootstrap",
                connector,
                manifest_id,
                import_id_ack,
                deferred_verification_ack,
            )
        )
        manifest.status = "running"
        await asyncio.sleep(0)
        manifest.status = "completed"

    service.execute_additions_first_bootstrap = execute_additions_first_bootstrap
    path = (
        f"/classroom/imports/manifest/{MANIFEST_ID}"
        "/additions-first-bootstrap"
    )

    wrong_id = client.post(
        path,
        data={
            "import_id_ack": "wrong-import",
            "deferred_verification_ack": "on",
        },
    )
    assert "OR-CONFIRMATION-MISMATCH" in wrong_id.text

    missing_ack = client.post(path, data={"import_id_ack": "import-1"})
    assert "OR-DEFERRED-VERIFICATION-ACK-REQUIRED" in missing_ack.text
    assert not any(
        call[0] == "execute_additions_first_bootstrap"
        for call in service.calls
    )

    queued = client.post(
        path,
        data={
            "import_id_ack": "import-1",
            "deferred_verification_ack": "on",
        },
    )
    assert queued.status_code == 200
    assert "Additions-first bootstrap was queued" in queued.text
    assert "course creation, student additions, activation" in queued.text
    assert "course creation, activation, student additions" not in queued.text
    assert 'hx-trigger="load delay:750ms, every 3s"' in queued.text

    status = client.get(f"/classroom/imports/manifest/{MANIFEST_ID}/status")
    assert status.status_code == 200
    assert any(
        call[0] == "execute_additions_first_bootstrap"
        and call[2:] == (MANIFEST_ID, "import-1", True)
        for call in service.calls
    )


def test_bootstrap_route_rejects_destructive_or_nonpaused_manifest_server_side():
    client, service, manifest = _paused_limited_manifest()

    async def should_not_run(*args, **kwargs):
        service.calls.append(("execute_additions_first_bootstrap", args, kwargs))

    service.execute_additions_first_bootstrap = should_not_run
    manifest.actions[0].kind = "course_archive"
    response = client.post(
        f"/classroom/imports/manifest/{MANIFEST_ID}/additions-first-bootstrap",
        data={
            "import_id_ack": "import-1",
            "deferred_verification_ack": "on",
        },
    )

    assert "OR-BOOTSTRAP-NOT-ELIGIBLE" in response.text
    assert not any(
        call[0] == "execute_additions_first_bootstrap"
        for call in service.calls
    )


def test_manifest_surfaces_reconciling_and_partial_status_text():
    client, service, manifest = _paused_limited_manifest()
    original_progress = service.get_execution_progress

    def reconciling_progress(manifest_id):
        value = original_progress(manifest_id)
        value.run.phase = "reconciliation"
        return value

    service.get_execution_progress = reconciling_progress
    manifest.status = "running"
    reconciling = client.get(f"/classroom/imports/manifest/{MANIFEST_ID}")
    assert (
        "deferred verification and bounded retries are reconciling"
        in reconciling.text.casefold()
    )

    manifest.status = "partial"
    partial = client.get(f"/classroom/imports/manifest/{MANIFEST_ID}")
    assert "Partial progress is durable" in partial.text


def test_additions_first_progress_uses_dispatch_and_submitted_semantics():
    client, service, manifest = _paused_limited_manifest()
    original_progress = service.get_execution_progress

    def additions_first_progress(manifest_id):
        value = original_progress(manifest_id)
        value.run.status = "pause_requested"
        value.submitted = 125
        value.current_batch.execution_mode = "additions_first"
        value.current_batch.native_progress_count = 37
        value.current_batch.native_progress_total = 50
        return value

    service.get_execution_progress = additions_first_progress
    manifest.status = "running"
    response = client.get(f"/classroom/imports/manifest/{MANIFEST_ID}")

    assert "125 submitted awaiting verification" in response.text
    assert "dispatched 37 / 50 to GAM" in response.text
    assert "Current batch" not in response.text
    assert "actions/min" not in response.text
    assert "Estimated remaining" not in response.text
    assert "active whole phase will finish before stopping" in response.text
    assert "active batch is being verified" not in response.text
    assert "Stop safely after current phase" in response.text
    assert "Stop safely after current batch" not in response.text


def test_ordinary_progress_keeps_verified_batch_metrics_and_pause_copy():
    client, service, manifest = _paused_limited_manifest()
    original_progress = service.get_execution_progress

    def paused_verified_progress(manifest_id):
        value = original_progress(manifest_id)
        value.run.status = "pause_requested"
        return value

    service.get_execution_progress = paused_verified_progress
    manifest.status = "running"
    response = client.get(f"/classroom/imports/manifest/{MANIFEST_ID}")

    assert "Current batch" in response.text
    assert "112.0 actions/min" in response.text
    assert "Estimated remaining" in response.text
    assert "active batch is being verified" in response.text
    assert "Stop safely after current batch" in response.text
    assert "submitted awaiting verification" not in response.text


def test_missing_report_link_and_count_only_render_when_rows_exist():
    client, service, _ = _paused_limited_manifest()
    store = _MissingReportStore()
    service.store = store

    empty = client.get(f"/classroom/imports/manifest/{MANIFEST_ID}")
    assert "Download missing-action CSV" not in empty.text

    store.count = 2
    populated = client.get(f"/classroom/imports/manifest/{MANIFEST_ID}")
    assert "2</span> additive actions" in populated.text
    assert "Download missing-action CSV" in populated.text
    assert (
        f'href="/classroom/imports/manifest/{MANIFEST_ID}'
        '/additions-first-missing.csv"'
    ) in populated.text
    workspace = client.get("/classroom/imports")
    assert "2</span> additive actions" in workspace.text
    assert "Download missing-action CSV" in workspace.text
    assert ("count", MANIFEST_ID) in store.calls


def test_missing_report_download_streams_store_csv_with_private_headers():
    client, service, _ = _paused_limited_manifest()
    store = _MissingReportStore(count=2)
    service.store = store

    response = client.get(
        f"/classroom/imports/manifest/{MANIFEST_ID}"
        "/additions-first-missing.csv"
    )

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-oneroster-row-count"] == "2"
    assert response.headers["content-disposition"] == (
        'attachment; filename="oneroster-additions-first-missing-'
        f'{MANIFEST_ID}.csv"'
    )
    assert response.text.startswith("action_id,kind,reason,cycle\n")
    assert "action-1,student_add,not visible,2" in response.text
    assert store.calls[-2:] == [
        ("count", MANIFEST_ID),
        ("write", MANIFEST_ID),
    ]
