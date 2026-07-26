from __future__ import annotations

import csv
import io
import zipfile
from typing import Mapping, Sequence


def csv_text(headers: Sequence[str], rows: Sequence[Mapping[str, object]]) -> str:
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=list(headers), lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue()


def valid_files(
    *,
    extra_users: int = 0,
    primary: str = "true",
    include_teacher: bool = True,
    term_ids: str = "term-1",
) -> dict[str, str]:
    users = [
        {
            "sourcedId": "teacher-1",
            "status": "active",
            "username": "teacher",
            "email": "teacher@example.org",
            "givenName": "Ada",
            "familyName": "Teacher",
            "identifier": "t1",
            "orgSourcedIds": "school-1",
        },
        {
            "sourcedId": "student-1",
            "status": "active",
            "username": "student",
            "email": "student@example.org",
            "givenName": "Sam",
            "familyName": "Student",
            "identifier": "s1",
            "orgSourcedIds": "school-1",
        },
    ]
    enrollments = [
        {
            "sourcedId": "enrollment-student-1",
            "status": "active",
            "classSourcedId": "101",
            "schoolSourcedId": "school-1",
            "userSourcedId": "student-1",
            "role": "student",
            "primary": "false",
            "beginDate": "",
            "endDate": "",
        }
    ]
    if include_teacher:
        enrollments.insert(
            0,
            {
                "sourcedId": "enrollment-teacher-1",
                "status": "active",
                "classSourcedId": "101",
                "schoolSourcedId": "school-1",
                "userSourcedId": "teacher-1",
                "role": "teacher",
                "primary": primary,
                "beginDate": "",
                "endDate": "",
            },
        )
    for number in range(extra_users):
        source_id = f"student-{number + 2}"
        users.append(
            {
                "sourcedId": source_id,
                "status": "active",
                "username": source_id,
                "email": f"{source_id}@example.org",
                "givenName": "Student",
                "familyName": f"{number + 2:03d}",
                "identifier": source_id,
                "orgSourcedIds": "school-1",
            }
        )
        enrollments.append(
            {
                "sourcedId": f"enrollment-{source_id}",
                "status": "active",
                "classSourcedId": "101",
                "schoolSourcedId": "school-1",
                "userSourcedId": source_id,
                "role": "student",
                "primary": "false",
                "beginDate": "",
                "endDate": "",
            }
        )

    manifest_rows = [
        {"propertyName": "manifest.version", "value": "1.0"},
        {"propertyName": "oneroster.version", "value": "1.1"},
        {"propertyName": "file.academicSessions", "value": "bulk"},
        {"propertyName": "file.classes", "value": "bulk"},
        {"propertyName": "file.courses", "value": "bulk"},
        {"propertyName": "file.enrollments", "value": "bulk"},
        {"propertyName": "file.orgs", "value": "bulk"},
        {"propertyName": "file.users", "value": "bulk"},
    ]
    files = {
        "manifest.csv": csv_text(("propertyName", "value"), manifest_rows),
        "academicSessions.csv": csv_text(
            (
                "sourcedId",
                "status",
                "title",
                "type",
                "startDate",
                "endDate",
                "parentSourcedId",
                "schoolYear",
            ),
            [
                {
                    "sourcedId": "year-1",
                    "status": "active",
                    "title": "School Year",
                    "type": "schoolYear",
                    "startDate": "2000-01-01",
                    "endDate": "2100-12-31",
                    "parentSourcedId": "",
                    "schoolYear": "2026-27",
                },
                {
                    "sourcedId": "term-1",
                    "status": "active",
                    "title": "Current Term",
                    "type": "term",
                    "startDate": "2000-01-01",
                    "endDate": "2100-12-31",
                    "parentSourcedId": "year-1",
                    "schoolYear": "2026-27",
                },
            ],
        ),
        "orgs.csv": csv_text(
            ("sourcedId", "status", "name", "type", "parentSourcedId"),
            [
                {
                    "sourcedId": "school-1",
                    "status": "active",
                    "name": "Example School",
                    "type": "school",
                    "parentSourcedId": "",
                }
            ],
        ),
        "users.csv": csv_text(
            (
                "sourcedId",
                "status",
                "username",
                "email",
                "givenName",
                "familyName",
                "identifier",
                "orgSourcedIds",
            ),
            users,
        ),
        "courses.csv": csv_text(
            (
                "sourcedId",
                "status",
                "title",
                "schoolYearSourcedId",
                "orgSourcedId",
                "grades",
            ),
            [
                {
                    "sourcedId": "course-1",
                    "status": "active",
                    "title": "Algebra I",
                    "schoolYearSourcedId": "year-1",
                    "orgSourcedId": "school-1",
                    "grades": "9",
                }
            ],
        ),
        "classes.csv": csv_text(
            (
                "sourcedId",
                "status",
                "title",
                "classCode",
                "location",
                "courseSourcedId",
                "terms",
                "schoolSourcedId",
                "grades",
            ),
            [
                {
                    "sourcedId": "101",
                    "status": "active",
                    "title": "Algebra Section",
                    "classCode": "P1",
                    "location": "101",
                    "courseSourcedId": "course-1",
                    "terms": term_ids,
                    "schoolSourcedId": "school-1",
                    "grades": "9",
                }
            ],
        ),
        "enrollments.csv": csv_text(
            (
                "sourcedId",
                "status",
                "classSourcedId",
                "schoolSourcedId",
                "userSourcedId",
                "role",
                "primary",
                "beginDate",
                "endDate",
            ),
            enrollments,
        ),
    }
    return files


def zip_bytes(files: Mapping[str, str], *, compression: int = zipfile.ZIP_DEFLATED) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=compression) as archive:
        for name, value in files.items():
            archive.writestr(name, value.encode("utf-8"))
    return output.getvalue()
