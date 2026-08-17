from __future__ import annotations

import asyncio
import ctypes
import json
import os
import signal
import sys
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from gamgui.core.audit import AuditLog
from gamgui.core.connectors.gam_connector import (
    GAMConnector,
    NativeClassroomBatchError,
    ONEROSTER_NATIVE_BATCH_TIMEOUT,
)
from gamgui.core.gam.commands import COURSE_INDEX_FIELDS, GAMCommands
from gamgui.core.gam.errors import GAMError, GAMErrorKind
from gamgui.core.gam.runner import (
    GAMRunner,
    StreamingGAMError,
    StreamingRunResult,
)

pytestmark = pytest.mark.asyncio


def _pid_exists(pid: int) -> bool:
    if os.name == "nt":
        synchronize = 0x00100000
        wait_timeout = 0x00000102
        handle = ctypes.windll.kernel32.OpenProcess(synchronize, False, pid)
        if not handle:
            return False
        try:
            return ctypes.windll.kernel32.WaitForSingleObject(handle, 0) == wait_timeout
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


async def test_authenticated_streaming_drains_lines_and_bounds_tails(
    vault,
    tmp_path: Path,
    domain: str,
):
    runner = GAMRunner(
        vault=vault,
        gam_binary=Path(sys.executable),
        base_dir=tmp_path,
        timeout=15,
    )
    observed: list[tuple[str, str]] = []

    async def observe(stream_name: str, line: str) -> None:
        await asyncio.sleep(0)
        observed.append((stream_name, line))

    script = (
        "import os,sys;"
        "[print(f'out-{i}', flush=True) for i in range(5)];"
        "[print(f'err-{i}', file=sys.stderr, flush=True) for i in range(5)];"
        "print('threads='+os.environ['GAM_THREADS'], flush=True)"
    )
    result = await runner.run_authenticated_streaming(
        domain,
        ["-c", script],
        timeout=15,
        gam_threads=10,
        line_callback=observe,
        tail_lines=2,
    )

    assert result.returncode == 0
    assert result.stdout_tail == ("out-4", "threads=10")
    assert result.stderr_tail == ("err-3", "err-4")
    assert ("stdout", "threads=10") in observed
    assert ("stderr", "err-4") in observed
    assert not list(tmp_path.glob("gamcfg-*"))


async def test_authenticated_streaming_failure_carries_bounded_result(
    vault,
    tmp_path: Path,
    domain: str,
):
    runner = GAMRunner(
        vault=vault,
        gam_binary=Path(sys.executable),
        base_dir=tmp_path,
        timeout=15,
    )
    script = "import sys; print('rate limit exceeded', file=sys.stderr); sys.exit(7)"

    with pytest.raises(StreamingGAMError) as caught:
        await runner.run_authenticated_streaming(
            domain,
            ["-c", script],
            timeout=15,
            tail_lines=2,
        )

    assert caught.value.kind is GAMErrorKind.RATE_LIMITED
    assert caught.value.result.returncode == 7
    assert caught.value.result.stderr_tail == ("rate limit exceeded",)


async def test_authenticated_streaming_timeout_terminates_pipe_inheriting_child(
    vault,
    tmp_path: Path,
    domain: str,
):
    runner = GAMRunner(
        vault=vault,
        gam_binary=Path(sys.executable),
        base_dir=tmp_path,
        timeout=15,
    )
    child_pid_path = tmp_path / "inherited-pipe-child.pid"
    child_script = "import time; time.sleep(60)"
    script = (
        "import pathlib,subprocess,sys,time;"
        f"child=subprocess.Popen([sys.executable,'-c',{child_script!r}]);"
        f"pathlib.Path({str(child_pid_path)!r}).write_text(str(child.pid),encoding='ascii');"
        "print('child-ready',flush=True);"
        "time.sleep(60)"
    )
    child_pid = 0
    try:
        with pytest.raises(GAMError) as caught:
            await asyncio.wait_for(
                runner.run_authenticated_streaming(
                    domain,
                    ["-c", script],
                    timeout=2,
                ),
                timeout=15,
            )

        assert caught.value.kind is GAMErrorKind.TIMEOUT
        assert child_pid_path.exists()
        child_pid = int(child_pid_path.read_text(encoding="ascii"))
        for _ in range(100):
            if not _pid_exists(child_pid):
                break
            await asyncio.sleep(0.05)
        assert not _pid_exists(child_pid)
        assert not list(tmp_path.glob("gamcfg-*"))
    finally:
        if not child_pid and child_pid_path.exists():
            child_pid = int(child_pid_path.read_text(encoding="ascii"))
        if child_pid and _pid_exists(child_pid):
            try:
                os.kill(child_pid, signal.SIGTERM)
            except OSError:
                pass


class _NativeBatchRunner:
    def __init__(self, root: Path, *, fail: bool = False) -> None:
        self.base_dir = root
        self.timeout = 15.0
        self.fail = fail
        self.calls: list[tuple[str, list[str], dict]] = []
        self.batch_path: Path | None = None
        self.line_count = 0
        self.first_line = ""
        self.last_line = ""

    async def run_authenticated_streaming(self, domain, argv, **kwargs):
        self.calls.append((domain, list(argv), dict(kwargs)))
        self.batch_path = Path(argv[1])
        with self.batch_path.open("r", encoding="utf-8") as stream:
            for raw_line in stream:
                line = raw_line.rstrip("\r\n")
                if not self.line_count:
                    self.first_line = line
                self.last_line = line
                self.line_count += 1
        callback = kwargs["line_callback"]
        total = self.line_count
        await callback("stderr", "08/13/2026,not progress")
        await callback("stderr", f"2026-08-13T20:00:00,0,Processing item 1/{total}")
        await callback(
            "stderr",
            f"2026-08-13T20:00:01,0,Processing item {total}/{total}",
        )
        result = StreamingRunResult(
            returncode=7 if self.fail else 0,
            stdout_tail=("bounded stdout",),
            stderr_tail=("rate limit exceeded",) if self.fail else (),
            duration_seconds=1.25,
        )
        if self.fail:
            cause = GAMError.from_run(
                result.returncode,
                "\n".join(result.stderr_tail),
                list(argv),
            )
            raise StreamingGAMError(cause, result)
        return result


class _OnePassCommands:
    def __init__(self, count: int) -> None:
        self.count = count
        self.iterations = 0
        self.thread_id: int | None = None

    def __iter__(self):
        if self.iterations:
            raise AssertionError("command source was iterated more than once")
        self.iterations += 1
        self.thread_id = threading.get_ident()
        for index in range(self.count):
            yield [
                "course",
                f"d:Section_{index}",
                "add",
                "students",
                f"student-{index}@example.com",
            ]


async def test_native_phase_batch_streams_once_and_launches_one_process(
    tmp_path: Path,
    domain: str,
):
    runner = _NativeBatchRunner(tmp_path)
    connector = GAMConnector(
        runner=runner,  # type: ignore[arg-type]
        domain=domain,
        audit=AuditLog(tmp_path / "audit.jsonl"),
    )
    commands = _OnePassCommands(100_001)
    caller_thread = threading.get_ident()
    progress: list[tuple[int, int]] = []

    receipt = await connector.run_classroom_phase_batch(
        commands,
        progress_callback=lambda item: progress.append((item.dispatched, item.total)),
    )

    assert commands.iterations == 1
    assert commands.thread_id != caller_thread
    assert len(runner.calls) == 1
    assert runner.calls[0][1][0] == "batch"
    assert runner.calls[0][1][-2:] == ["showcmds", "false"]
    assert runner.calls[0][2]["serialize"] is True
    assert runner.calls[0][2]["gam_threads"] == 10
    assert runner.calls[0][2]["timeout"] == ONEROSTER_NATIVE_BATCH_TIMEOUT
    assert ONEROSTER_NATIVE_BATCH_TIMEOUT == 24 * 60 * 60
    assert receipt.submitted_count == 100_001
    assert receipt.process_result.stdout_tail == ("bounded stdout",)
    assert receipt.progress_count == 100_001
    assert progress == [(1, 100_001), (100_001, 100_001)]
    assert runner.line_count == 100_001
    assert runner.first_line.startswith("gam course d:Section_0 add students")
    assert runner.last_line.startswith("gam course d:Section_100000 add students")
    assert runner.batch_path is not None and not runner.batch_path.exists()


async def test_native_phase_batch_rejects_invalid_command_before_launch(
    tmp_path: Path,
    domain: str,
):
    runner = _NativeBatchRunner(tmp_path)
    connector = GAMConnector(
        runner=runner,  # type: ignore[arg-type]
        domain=domain,
        audit=AuditLog(tmp_path / "audit.jsonl"),
    )

    with pytest.raises(ValueError, match="outside the OneRoster allowlist"):
        await connector.run_classroom_phase_batch(
            iter(
                (
                    ["course", "d:Section_1", "add", "students", "one@example.com"],
                    ["delete", "user", "one@example.com"],
                )
            )
        )

    assert runner.calls == []
    assert not list(tmp_path.glob("gamgui-oneroster-native-batch-*"))


async def test_native_phase_batch_wraps_gam_failure_and_cleans_file(
    tmp_path: Path,
    domain: str,
):
    runner = _NativeBatchRunner(tmp_path, fail=True)
    connector = GAMConnector(
        runner=runner,  # type: ignore[arg-type]
        domain=domain,
        audit=AuditLog(tmp_path / "audit.jsonl"),
    )

    with pytest.raises(NativeClassroomBatchError) as caught:
        await connector.run_classroom_phase_batch(
            (["course", "d:Section_1", "add", "students", "one@example.com"],)
        )

    assert caught.value.error_code == "GAM-NATIVE-BATCH-FAILED"
    assert caught.value.kind is GAMErrorKind.RATE_LIMITED
    assert caught.value.submitted_count == 1
    assert caught.value.process_result is not None
    assert caught.value.argv == ["batch", "<private-batch>", "showcmds", "false"]
    assert runner.batch_path is not None and not runner.batch_path.exists()


class _InventoryRunner:
    def __init__(self, root: Path, rows: list[dict]) -> None:
        self.base_dir = root
        self.rows = rows
        self.calls: list[tuple[str, list[str], dict]] = []

    @asynccontextmanager
    async def run_authenticated_to_file(self, domain, argv, **kwargs):
        self.calls.append((domain, list(argv), dict(kwargs)))
        path = self.base_dir / "course-inventory.ndjson"
        path.write_text(
            "\n".join(json.dumps(row) for row in self.rows),
            encoding="utf-8",
        )
        os.chmod(path, 0o600)
        try:
            yield SimpleNamespace(path=path, stdout_bytes=path.stat().st_size)
        finally:
            path.unlink(missing_ok=True)


async def test_course_snapshot_filters_requested_aliases_in_one_process(
    tmp_path: Path,
    domain: str,
):
    runner = _InventoryRunner(
        tmp_path,
        [
            {"id": "other", "name": "Other", "aliases": []},
            {"id": "course-b", "name": "B", "aliases": [{"alias": "Section_B"}]},
            {"id": "course-a", "name": "A", "aliases": [{"alias": "d:Section_A"}]},
        ],
    )
    connector = GAMConnector(
        runner=runner,  # type: ignore[arg-type]
        domain=domain,
        audit=AuditLog(tmp_path / "audit.jsonl"),
    )

    courses = await connector.snapshot_oneroster_managed_courses(
        ["Section_A", "d:Section_missing", "d:Section_B"]
    )

    assert [course.id for course in courses] == ["course-a", "course-b"]
    assert [course.aliases for course in courses] == [
        ("d:Section_A",),
        ("d:Section_B",),
    ]
    assert len(runner.calls) == 1
    assert runner.calls[0][1] == [
        "print",
        "courses",
        "aliases",
        "fields",
        ",".join(COURSE_INDEX_FIELDS),
        "formatjson",
    ]


@pytest.mark.parametrize(
    "rows",
    [
        [
            {"id": "course-a", "aliases": [{"alias": "Section_A"}]},
            {"id": "course-b", "aliases": [{"alias": "Section_A"}]},
        ],
        [
            {"id": "same-course", "aliases": [{"alias": "Section_A"}]},
            {"id": "same-course", "aliases": [{"alias": "Section_B"}]},
        ],
        [
            {
                "id": "same-course",
                "aliases": [
                    {"alias": "Section_A"},
                    {"alias": "Section_B"},
                ],
            },
        ],
        [
            {"aliases": [{"alias": "Section_A"}]},
        ],
    ],
)
async def test_course_snapshot_rejects_ambiguous_alias_or_course_id(
    tmp_path: Path,
    domain: str,
    rows: list[dict],
):
    runner = _InventoryRunner(tmp_path, rows)
    connector = GAMConnector(
        runner=runner,  # type: ignore[arg-type]
        domain=domain,
        audit=AuditLog(tmp_path / "audit.jsonl"),
    )

    with pytest.raises(ValueError, match="Managed course inventory"):
        await connector.snapshot_oneroster_managed_courses(
            ["Section_A", "Section_B"]
        )
