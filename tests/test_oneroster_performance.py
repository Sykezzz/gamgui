from __future__ import annotations

import asyncio
import statistics
import time
from collections import Counter
from pathlib import Path

import pytest

from gamgui.components.oneroster import OneRosterService, ThresholdProfile
from gamgui.components.oneroster.executor import OneRosterExecutor
from gamgui.components.oneroster.models import ImportAction
from gamgui.core.gam.models import GAMUser
from tests.test_oneroster_helpers import valid_files, zip_bytes


class _DelayedLiveSnapshots:
    def __init__(self, *, delay: float, serialize_snapshots: bool) -> None:
        self.delay = delay
        self.lock = asyncio.Lock() if serialize_snapshots else None
        self.calls: Counter[str] = Counter()
        self.users = {
            "teacher@example.org": GAMUser("teacher@example.org", user_id="teacher-id"),
            "student@example.org": GAMUser("student@example.org", user_id="student-id"),
            **{
                f"student-{number}@example.org": GAMUser(
                    f"student-{number}@example.org",
                    user_id=f"student-{number}-id",
                )
                for number in range(2, 2002)
            },
        }

    async def _delay(self) -> None:
        if self.lock is None:
            await asyncio.sleep(self.delay)
            return
        async with self.lock:
            await asyncio.sleep(self.delay)

    async def list_oneroster_directory(self):
        self.calls["directory"] += 1
        await self._delay()
        return dict(self.users)

    async def list_oneroster_managed_courses(self, _aliases):
        self.calls["courses"] += 1
        await self._delay()
        return []

    async def get_course(self, *_args, **_kwargs):
        raise KeyError("new course")


def _ready_service(root: Path) -> tuple[OneRosterService, str]:
    service = OneRosterService("example.org", root)
    snapshot = service.upload(zip_bytes(valid_files(extra_users=2000)))
    service.save_threshold_profile(ThresholdProfile(configured=True))
    return service, snapshot.id


async def _timed_plan(root: Path, *, serialize_snapshots: bool):
    service, import_id = _ready_service(root)
    connector = _DelayedLiveSnapshots(
        delay=0.25,
        serialize_snapshots=serialize_snapshots,
    )
    started = time.perf_counter()
    planning = await service.build_live_plan(connector, import_id)
    elapsed = time.perf_counter() - started
    action_identity = tuple(
        (action.id, action.kind, action.subject, action.target)
        for action in planning.actions
    )
    return elapsed, action_identity, connector.calls, planning.performance


def _prepared_execution_batch(root: Path):
    service = OneRosterService("example.org", root)
    snapshot = service.upload(zip_bytes(valid_files()))
    service.save_threshold_profile(ThresholdProfile(configured=True))
    actions = tuple(
        ImportAction(
            f"action-{number:03d}",
            "student_add",
            "Section_101",
            f"student-{number:03d}@example.org",
        )
        for number in range(50)
    )
    manifest = service.create_manifest(
        snapshot.id,
        config_hash="config",
        live_hash="live",
        actions=actions,
    )
    executor = OneRosterExecutor(service.store, object())  # type: ignore[arg-type]
    service.store.confirm_manifest(manifest.id, manifest.import_id)
    service.store.claim_manifest(
        manifest.id,
        owner_id=executor._operation_owner,
        owner_identity=executor._operation_identity,
    )
    run = service.store.start_execution_run(
        manifest.id,
        phase="student_add",
        owner_id=executor._operation_owner,
    )
    batch = service.store.prepare_execution_batch(
        run.id,
        manifest.id,
        actions,
        phase="student_add",
        owner_id=executor._operation_owner,
    )
    service.store.mark_execution_batch_started(batch.id)
    return service, manifest, batch, executor, actions


@pytest.mark.asyncio
async def test_large_roster_planning_gate_improves_median_without_plan_drift(
    tmp_path: Path,
):
    """Evidence gate for the old serial snapshots versus the concurrent implementation."""

    serial_times: list[float] = []
    optimized_times: list[float] = []
    for run_number in range(3):
        serial = await _timed_plan(
            tmp_path / f"serial-{run_number}",
            serialize_snapshots=True,
        )
        optimized = await _timed_plan(
            tmp_path / f"optimized-{run_number}",
            serialize_snapshots=False,
        )
        serial_times.append(serial[0])
        optimized_times.append(optimized[0])
        assert optimized[1] == serial[1]
        assert optimized[2] == serial[2] == Counter(directory=1, courses=1)
        assert optimized[3].directory_snapshot_seconds > 0
        assert optimized[3].classroom_snapshot_seconds > 0

    serial_median = statistics.median(serial_times)
    optimized_median = statistics.median(optimized_times)
    improvement = (serial_median - optimized_median) / serial_median

    assert improvement >= 0.30, {
        "serial_median": serial_median,
        "optimized_median": optimized_median,
        "improvement": improvement,
    }


def test_atomic_batch_persistence_gate_improves_median_with_identical_outcomes(
    tmp_path: Path,
):
    legacy_times: list[float] = []
    atomic_times: list[float] = []
    for run_number in range(3):
        legacy = _prepared_execution_batch(tmp_path / f"legacy-{run_number}")
        service, manifest, batch, executor, actions = legacy
        started = time.perf_counter()
        for action in actions:
            service.store.mark_action_result(
                manifest.id,
                action.id,
                status="applied",
                detail="verified",
                load_manifest=False,
                owner_id=executor._operation_owner,
            )
        service.store.finish_execution_batch(batch.id, status="completed")
        legacy_times.append(time.perf_counter() - started)
        legacy_progress = service.get_execution_progress(manifest.id)

        atomic = _prepared_execution_batch(tmp_path / f"atomic-{run_number}")
        service, manifest, batch, executor, actions = atomic
        started = time.perf_counter()
        service.store.complete_verified_batch(
            batch.id,
            manifest.id,
            {action.id: ("applied", "verified") for action in actions},
            owner_id=executor._operation_owner,
            apply_seconds=1.0,
            verification_seconds=1.0,
            verification_attempts=1,
            worker_count=5,
        )
        atomic_times.append(time.perf_counter() - started)
        atomic_progress = service.get_execution_progress(manifest.id)

        assert atomic_progress.applied == legacy_progress.applied == 50
        assert atomic_progress.failed == legacy_progress.failed == 0
        assert atomic_progress.pending == legacy_progress.pending == 0

    legacy_median = statistics.median(legacy_times)
    atomic_median = statistics.median(atomic_times)
    improvement = (legacy_median - atomic_median) / legacy_median
    assert improvement >= 0.30, {
        "legacy_median": legacy_median,
        "atomic_median": atomic_median,
        "improvement": improvement,
    }
