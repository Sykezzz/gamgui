from __future__ import annotations

import csv
import io
import json
import os
import stat
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from gamgui.core.connectors.gam_connector import GAMConnector
from gamgui.core.gam.commands import (
    COURSE_INDEX_FIELDS,
    ONEROSTER_DIRECTORY_FIELDS,
    GAMCommands,
)

pytestmark = pytest.mark.asyncio


class PrivateSpoolRunner:
    def __init__(self, root: Path, payload: str) -> None:
        self.root = root
        self.base_dir = root
        self.payload = payload
        self.calls: list[tuple[str, list[str]]] = []
        self.paths: list[Path] = []
        self.modes: list[int] = []

    @asynccontextmanager
    async def run_authenticated_to_file(self, domain, argv, **_kwargs):
        path = self.root / f"oneroster-{len(self.calls)}.ndjson"
        path.write_text(self.payload, encoding="utf-8")
        os.chmod(path, 0o600)
        self.calls.append((domain, list(argv)))
        self.paths.append(path)
        self.modes.append(stat.S_IMODE(path.stat().st_mode))
        try:
            yield SimpleNamespace(path=path, stdout_bytes=path.stat().st_size)
        finally:
            path.unlink(missing_ok=True)

    async def run_authenticated(self, *_args, **_kwargs):
        raise AssertionError("OneRoster bulk reads must not buffer GAM stdout.")


class AliasLookupRunner(PrivateSpoolRunner):
    def __init__(self, root: Path, courses: dict[str, dict]) -> None:
        super().__init__(root, "")
        self.courses = courses
        self.selector_paths: list[Path] = []
        self.selector_values: list[list[str]] = []
        self.selector_modes: list[int] = []

    @asynccontextmanager
    async def run_authenticated_to_file(self, domain, argv, **_kwargs):
        selector = Path(argv[argv.index("file") + 1])
        aliases = selector.read_text(encoding="utf-8").splitlines()
        self.selector_paths.append(selector)
        self.selector_values.append(aliases)
        self.selector_modes.append(stat.S_IMODE(selector.stat().st_mode))
        rows = [
            {
                "id": self.courses[alias]["id"],
                "course": self.courses[alias],
                "aliases": self.courses[alias].get("aliases", [alias]),
            }
            for alias in aliases
            if alias in self.courses
        ]
        path = self.root / f"oneroster-{len(self.calls)}.csv"
        path.write_text(_formatjson_courses_csv(rows), encoding="utf-8")
        os.chmod(path, 0o600)
        self.calls.append((domain, list(argv)))
        self.paths.append(path)
        self.modes.append(stat.S_IMODE(path.stat().st_mode))
        try:
            yield SimpleNamespace(path=path, stdout_bytes=path.stat().st_size)
        finally:
            path.unlink(missing_ok=True)


def _formatjson_participants_csv(rows: list[dict]) -> str:
    output = io.StringIO(newline="")
    writer = csv.DictWriter(
        output,
        fieldnames=[
            "courseId",
            "courseName",
            "JSON-teachers",
            "JSON-students",
        ],
    )
    writer.writeheader()
    for row in rows:
        writer.writerow(
            {
                "courseId": row["courseId"],
                "courseName": row.get("courseName", ""),
                "JSON-teachers": json.dumps(row.get("teachers", [])),
                "JSON-students": json.dumps(row.get("students", [])),
            }
        )
    return output.getvalue()


def _formatjson_courses_csv(rows: list[dict]) -> str:
    output = io.StringIO(newline="")
    writer = csv.DictWriter(
        output,
        fieldnames=["id", "JSON", "JSON-aliases"],
    )
    writer.writeheader()
    for row in rows:
        writer.writerow(
            {
                "id": row["id"],
                "JSON": json.dumps(row["course"]),
                "JSON-aliases": json.dumps(
                    [{"alias": alias} for alias in row.get("aliases", [])]
                ),
            }
        )
    return output.getvalue()


async def test_oneroster_bulk_read_argv_is_exact():
    assert GAMCommands.print_oneroster_directory() == [
        "print",
        "users",
        "fields",
        ",".join(ONEROSTER_DIRECTORY_FIELDS),
        "formatjson",
    ]
    assert GAMCommands.print_oneroster_courses_file(
        "/private/managed-courses.txt"
    ) == [
        "print",
        "courses",
        "course",
        "file",
        "/private/managed-courses.txt",
        "aliases",
        "fields",
        ",".join(COURSE_INDEX_FIELDS),
        "formatjson",
    ]
    assert GAMCommands.print_course_participants_many(
        ["101", "202"],
        "all",
    ) == [
        "print",
        "course-participants",
        "course",
        "101",
        "course",
        "202",
        "show",
        "all",
        "formatjson",
    ]
    assert GAMCommands.print_course_participants_file(
        "/private/course-ids.txt",
        "students",
    ) == [
        "print",
        "course-participants",
        "course",
        "file",
        "/private/course-ids.txt",
        "show",
        "students",
        "formatjson",
    ]


async def test_oneroster_directory_is_alias_resolvable_and_parsed_off_loop(
    tmp_path: Path,
    monkeypatch,
):
    import gamgui.core.connectors.gam_connector as connector_module

    runner = PrivateSpoolRunner(
        tmp_path,
        (
            '{"id":"owner-1","primaryEmail":"Teacher@Example.org",'
            '"aliases":["TSmith@Example.org","teacher.legacy@example.org"],'
            '"suspended":false,"recoveryEmail":"must-not-survive"}\n'
            '{"primaryEmail":"Suspended@Example.org","aliases":[],"suspended":true}'
        ),
    )
    connector = GAMConnector(runner, "example.org")  # type: ignore[arg-type]
    caller_thread = threading.get_ident()
    parser_threads: list[int] = []
    real_reader = connector_module._read_oneroster_directory_spool

    def tracked_reader(path):
        parser_threads.append(threading.get_ident())
        return real_reader(path)

    monkeypatch.setattr(
        connector_module,
        "_read_oneroster_directory_spool",
        tracked_reader,
    )
    snapshot = await connector.list_oneroster_directory()

    teacher = snapshot["teacher@example.org"]
    assert snapshot["tsmith@example.org"] is teacher
    assert snapshot["teacher.legacy@example.org"] is teacher
    assert teacher.primary_email == "teacher@example.org"
    assert teacher.user_id == "owner-1"
    assert teacher.aliases == [
        "tsmith@example.org",
        "teacher.legacy@example.org",
    ]
    assert teacher.raw == {}
    assert snapshot["suspended@example.org"].suspended is True
    assert runner.calls == [
        ("example.org", GAMCommands.print_oneroster_directory())
    ]
    assert parser_threads and all(thread != caller_thread for thread in parser_threads)
    assert all(not path.exists() for path in runner.paths)
    if os.name != "nt":
        assert runner.modes == [0o600]


async def test_oneroster_courses_resolve_only_exact_requested_aliases(
    tmp_path: Path,
):
    runner = AliasLookupRunner(
        tmp_path,
        {
            "d:Section_101": {
                "id": "101",
                "name": "Algebra",
                "section": "P1",
                "room": "101",
                "ownerId": "owner-1",
                "courseState": "ACTIVE",
            },
            "d:Section_202": {
                "id": "202",
                "name": "Geometry",
                "ownerId": "owner-2",
                "courseState": "ARCHIVED",
            },
        },
    )
    connector = GAMConnector(runner, "example.org")  # type: ignore[arg-type]

    courses = await connector.list_oneroster_managed_courses(
        ["Section_101", "Section_202"]
    )

    assert [course.id for course in courses] == ["101", "202"]
    assert courses[0].aliases == ("d:Section_101",)
    assert courses[1].aliases == ("d:Section_202",)
    assert courses[0].owner_id == "owner-1"
    assert courses[0].owner_email == ""
    assert courses[0].description == ""
    assert courses[0].raw == {}
    assert len(runner.calls) == 1
    domain, argv = runner.calls[0]
    assert domain == "example.org"
    selector = Path(argv[argv.index("file") + 1])
    assert argv == GAMCommands.print_oneroster_courses_file(str(selector))
    assert runner.selector_values == [["d:Section_101", "d:Section_202"]]
    assert not selector.exists()
    assert all(not path.exists() for path in runner.paths)
    if os.name != "nt":
        assert runner.selector_modes == [0o600]


async def test_sparse_alias_results_are_mapped_without_recursive_gam_calls(
    tmp_path: Path,
):
    runner = AliasLookupRunner(
        tmp_path,
        {
            "d:Section_101": {"id": "101", "name": "One"},
            "d:Section_303": {"id": "303", "name": "Three"},
        },
    )
    connector = GAMConnector(runner, "example.org")  # type: ignore[arg-type]

    courses = await connector.list_oneroster_managed_courses(
        ["Section_101", "Section_202", "Section_303"]
    )

    assert [(course.id, course.aliases) for course in courses] == [
        ("101", ("d:Section_101",)),
        ("303", ("d:Section_303",)),
    ]
    assert len(runner.calls) == 1
    assert all(
        "owneremail" not in argv
        and "aliases" in argv
        and "states" not in argv
        for _domain, argv in runner.calls
    )


async def test_one_bounded_missing_alias_chunk_uses_one_private_selector_process(
    tmp_path: Path,
):
    runner = AliasLookupRunner(tmp_path, {})
    connector = GAMConnector(runner, "example.org")  # type: ignore[arg-type]
    aliases = [f"Section_{number}" for number in range(200)]

    courses = await connector.list_oneroster_managed_courses(aliases)

    assert courses == []
    assert len(runner.calls) == 1
    assert len(runner.selector_values) == 1
    assert runner.selector_values[0][0] == "d:Section_0"
    assert runner.selector_values[0][-1] == "d:Section_199"
    assert len(runner.selector_values[0]) == 200
    assert all(not path.exists() for path in runner.selector_paths)


async def test_two_aliases_resolving_to_one_course_fail_closed(tmp_path: Path):
    runner = AliasLookupRunner(
        tmp_path,
        {
            "d:Section_101": {"id": "same", "name": "One"},
            "d:Section_202": {"id": "same", "name": "Two"},
        },
    )
    connector = GAMConnector(runner, "example.org")  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="same Classroom course"):
        await connector.list_oneroster_managed_courses(
            ["Section_101", "Section_202"]
        )


async def test_oneroster_global_roster_filters_course_ids_and_role(tmp_path: Path):
    runner = PrivateSpoolRunner(
        tmp_path,
        _formatjson_participants_csv(
            [
                {
                    "courseId": "123",
                    "courseName": "Algebra",
                    "teachers": [
                        {
                            "userId": "teacher-1",
                            "profile": {"emailAddress": "teacher@example.org"},
                        }
                    ],
                    "students": [
                        {
                            "userId": "student-1",
                            "profile": {"emailAddress": "student@example.org"},
                        }
                    ],
                },
                {
                    "courseId": "456",
                    "courseName": "Geometry",
                    "students": [
                        {
                            "userId": "student-2",
                            "profile": {"emailAddress": "second@example.org"},
                        }
                    ],
                },
                {
                    "courseId": "999",
                    "courseName": "Unrequested",
                    "students": [
                        {
                            "userId": "student-3",
                            "profile": {"emailAddress": "unrequested@example.org"},
                        }
                    ],
                },
            ]
        ),
    )
    connector = GAMConnector(runner, "example.org")  # type: ignore[arg-type]

    participants = await connector.list_course_participants_many(
        ["123", "456"],
        "students",
    )

    assert {
        (participant.course_id, participant.email, participant.role)
        for participant in participants
    } == {
        ("123", "student@example.org", "students"),
        ("456", "second@example.org", "students"),
    }
    assert all(participant.raw == {} for participant in participants)
    assert len(runner.calls) == 1
    domain, argv = runner.calls[0]
    assert domain == "example.org"
    selector = Path(argv[argv.index("file") + 1])
    assert argv == GAMCommands.print_course_participants_file(
        str(selector),
        "students",
    )
    assert not selector.exists()
    assert all(not path.exists() for path in runner.paths)


async def test_oneroster_global_roster_invalid_nested_json_cleans_spool(tmp_path: Path):
    runner = PrivateSpoolRunner(
        tmp_path,
        (
            "courseId,courseName,JSON-teachers,JSON-students\r\n"
            '123,Algebra,not-json,"[]"\r\n'
        ),
    )
    connector = GAMConnector(runner, "example.org")  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="invalid JSON-teachers data"):
        await connector.list_course_participants_many(["123"], "all")

    assert len(runner.calls) == 1
    assert all(not path.exists() for path in runner.paths)


async def test_oneroster_global_roster_requires_every_requested_course_row(
    tmp_path: Path,
):
    runner = PrivateSpoolRunner(
        tmp_path,
        _formatjson_participants_csv(
            [
                {
                    "courseId": "123",
                    "courseName": "Algebra",
                    "teachers": [],
                    "students": [],
                }
            ]
        ),
    )
    connector = GAMConnector(runner, "example.org")  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="omitted"):
        await connector.list_course_participants_many(["123", "456"], "all")

    assert all(not path.exists() for path in runner.paths)


async def test_oneroster_global_roster_preserves_explicit_empty_course_coverage(
    tmp_path: Path,
):
    runner = PrivateSpoolRunner(
        tmp_path,
        _formatjson_participants_csv(
            [
                {
                    "courseId": "123",
                    "courseName": "Algebra",
                    "teachers": [],
                    "students": [],
                }
            ]
        ),
    )
    connector = GAMConnector(runner, "example.org")  # type: ignore[arg-type]

    snapshot = await connector.list_course_participants_many(["123"], "all")

    assert list(snapshot) == []
    assert snapshot.seen_course_ids == frozenset({"123"})
    assert snapshot.covers(["123"])


async def test_oneroster_global_roster_compacts_duplicate_members(tmp_path: Path):
    member = {
        "userId": "student-1",
        "profile": {"emailAddress": "student@example.org"},
    }
    runner = PrivateSpoolRunner(
        tmp_path,
        _formatjson_participants_csv(
            [
                {
                    "courseId": "123",
                    "courseName": "Algebra",
                    "teachers": [],
                    "students": [member, dict(member)],
                }
            ]
        ),
    )
    connector = GAMConnector(runner, "example.org")  # type: ignore[arg-type]

    snapshot = await connector.list_course_participants_many(["123"], "all")

    assert snapshot.for_course("123") == (
        frozenset(),
        frozenset({"student@example.org"}),
    )
    assert len(snapshot) == 1


async def test_oneroster_global_roster_fails_closed_at_membership_cap(
    tmp_path: Path,
    monkeypatch,
):
    monkeypatch.setattr(
        "gamgui.core.connectors.gam_connector.MAX_ONEROSTER_BULK_ROSTER_MEMBERS",
        1,
    )
    runner = PrivateSpoolRunner(
        tmp_path,
        _formatjson_participants_csv(
            [
                {
                    "courseId": "123",
                    "courseName": "Algebra",
                    "teachers": [],
                    "students": [
                        {
                            "userId": "student-1",
                            "profile": {"emailAddress": "first@example.org"},
                        },
                        {
                            "userId": "student-2",
                            "profile": {"emailAddress": "second@example.org"},
                        },
                    ],
                }
            ]
        ),
    )
    connector = GAMConnector(runner, "example.org")  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="safe membership limit"):
        await connector.list_course_participants_many(["123"], "all")

    assert all(not path.exists() for path in runner.paths)


@pytest.mark.parametrize(
    ("method_name", "args"),
    [
        ("list_oneroster_directory", ()),
        ("list_oneroster_managed_courses", (["Section_123"],)),
        ("list_course_participants_many", (["123"], "all")),
    ],
)
async def test_oneroster_bulk_parse_failure_cleans_private_spool(
    tmp_path: Path,
    method_name: str,
    args: tuple,
):
    runner = PrivateSpoolRunner(
        tmp_path,
        (
            '{"primaryEmail":"valid@example.org","id":"123","courseId":"123",'
            '"role":"student","aliases":["Section_123"],'
            '"profile":{"emailAddress":"student@example.org"}}\n'
            "not-json"
        ),
    )
    connector = GAMConnector(runner, "example.org")  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="invalid newline-delimited JSON"):
        await getattr(connector, method_name)(*args)

    assert len(runner.calls) == 1
    assert all(not path.exists() for path in runner.paths)
