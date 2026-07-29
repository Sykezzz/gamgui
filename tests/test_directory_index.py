from __future__ import annotations

import asyncio
import os
import sqlite3
import stat
import time

import pytest

from gamgui.core.directory_index import DirectoryIndex, MAX_PAGE_SIZE
from gamgui.core.gam.models import GAMGroup, GAMUser
from gamgui.web.server import AppState


def _user(n: int, domain: str = "example.com", suspended: bool = False) -> GAMUser:
    return GAMUser.from_json(
        {
            "primaryEmail": f"user{n:05d}@{domain}",
            "name": {"givenName": f"User{n:05d}", "familyName": f"Family{n % 100:02d}"},
            "suspended": suspended,
            "orgUnitPath": "/Students" if n % 2 else "/Staff",
            "organizations": [
                {
                    "title": "Library Media Specialist" if n == 42 else "Teacher",
                    "department": f"Campus {n % 20}",
                    "primary": True,
                }
            ],
        }
    )


def test_domain_isolation_and_reopen(tmp_path):
    path = tmp_path / "directory.db"
    first = DirectoryIndex(path, "first.example")
    second = DirectoryIndex(path, "second.example")
    first.replace_users([_user(1, "first.example")])
    second.replace_users([_user(2, "second.example")])

    assert [u.primary_email for u in first.search_users().items] == [
        "user00001@first.example"
    ]
    assert [u.primary_email for u in second.search_users().items] == [
        "user00002@second.example"
    ]
    reopened = DirectoryIndex(path, "first.example")
    assert reopened.status().users == 1
    assert reopened.search_users("user00001").total == 1


def test_user_pages_are_hard_bounded_and_cursor_driven(tmp_path):
    index = DirectoryIndex(tmp_path / "directory.db", "example.com")
    index.replace_users(_user(n) for n in range(125))

    first = index.search_users(limit=500)
    assert len(first.items) == MAX_PAGE_SIZE
    assert first.total == 125
    assert first.next_cursor
    second = index.search_users(limit=500, cursor=first.next_cursor)
    third = index.search_users(limit=500, cursor=second.next_cursor)
    assert len(second.items) == MAX_PAGE_SIZE
    assert len(third.items) == 25
    assert third.next_cursor is None
    assert len({u.primary_email for u in first.items + second.items + third.items}) == 125


def test_search_scope_snapshot_and_refresh_metadata(tmp_path):
    index = DirectoryIndex(tmp_path / "directory.db", "example.com")
    index.replace_users([_user(1), _user(2, suspended=True), _user(42)])

    assert index.search_users("library media").items[0].primary_email == "user00042@example.com"
    assert index.search_users("USER00001@EXAMPLE").total == 1
    assert index.search_users("family02", scope="suspended").total == 1
    assert index.search_users("", scope="active").total == 2
    assert index.search_users("%").total == 0
    assert index.search_users().snapshot_age_seconds is not None

    index.set_refreshing("users", True)
    assert index.search_users().refreshing is True
    index.set_refreshing("users", False)
    index.mark_stale("users")
    assert index.snapshot_age("users") is None
    assert index.is_stale("users")


def test_group_index_search_and_page_bound(tmp_path):
    index = DirectoryIndex(tmp_path / "directory.db", "example.com")
    groups = [
        GAMGroup(
            email=f"group{i:03d}@example.com",
            name=f"Campus Team {i:03d}",
            description="Counselors" if i == 12 else "",
            members_count=i,
        )
        for i in range(80)
    ]
    index.replace_groups(groups)

    page = index.search_groups(limit=999)
    assert len(page.items) == MAX_PAGE_SIZE and page.total == 80
    assert index.search_groups("counsel").items[0].email == "group012@example.com"
    assert index.status().groups == 80


def test_org_unit_search_is_distinct_bounded_and_uses_user_snapshot(tmp_path):
    index = DirectoryIndex(tmp_path / "directory.db", "example.com")
    users = [_user(n) for n in range(20)]
    users[0].org_unit_path = "/Staff/Teachers"
    users[1].org_unit_path = "/Staff/Teachers"
    index.replace_users(users)

    page = index.search_org_units("staff", limit=2)

    assert page.items == ["/Staff", "/Staff/Teachers"]
    assert page.total == 2
    assert page.next_cursor is None
    assert page.snapshot_age_seconds is not None


def test_empty_snapshot_is_distinct_from_never_refreshed(tmp_path):
    index = DirectoryIndex(tmp_path / "directory.db", "example.com")
    assert index.is_empty("groups")
    assert not index.has_snapshot("groups")

    index.replace_groups([])

    assert index.is_empty("groups")
    assert index.has_snapshot("groups")
    assert not index.is_stale("groups")


def test_failed_staging_refresh_preserves_previous_snapshot(tmp_path):
    index = DirectoryIndex(tmp_path / "directory.db", "example.com")
    index.replace_users([_user(1)])

    def broken():
        yield _user(2)
        raise RuntimeError("source stopped")

    with pytest.raises(RuntimeError, match="source stopped"):
        index.replace_users(broken())
    assert [u.primary_email for u in index.search_users().items] == [
        "user00001@example.com"
    ]


def test_upsert_and_remove_patch_only_one_row(tmp_path):
    index = DirectoryIndex(tmp_path / "directory.db", "example.com")
    index.replace_users([_user(1), _user(2)])
    replacement = _user(1)
    replacement.title = "Principal"
    index.upsert_user(replacement)
    assert index.search_users("principal").items[0].primary_email == "user00001@example.com"
    index.remove_user("USER00002@EXAMPLE.COM")
    assert index.search_users().total == 1


def test_search_uses_prefix_index(tmp_path):
    index = DirectoryIndex(tmp_path / "directory.db", "example.com")
    with sqlite3.connect(index.path) as conn:
        names = {row[1] for row in conn.execute("PRAGMA index_list('user_search_terms')")}
    assert "user_terms_prefix" in names


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits are not enforced on Windows")
def test_index_files_are_owner_only_where_supported(tmp_path):
    index = DirectoryIndex(tmp_path / "private" / "directory.db", "example.com")
    index.replace_users([_user(1)])
    assert stat.S_IMODE(index.path.stat().st_mode) & 0o077 == 0
    assert stat.S_IMODE(index.path.parent.stat().st_mode) & 0o077 == 0


def test_10k_indexed_search_stays_bounded_and_under_budget(tmp_path):
    index = DirectoryIndex(tmp_path / "directory.db", "example.com")
    index.replace_users(_user(n) for n in range(10_000))
    index.search_users("user09999")  # warm SQLite and OS page caches

    samples = []
    for query in ("user09999", "family42", "campus 7", "teacher") * 3:
        started = time.perf_counter()
        page = index.search_users(query, limit=50)
        samples.append(time.perf_counter() - started)
        assert len(page.items) <= MAX_PAGE_SIZE
    samples.sort()
    p95 = samples[int(len(samples) * 0.95) - 1]
    assert p95 < 0.100


async def test_app_state_initial_refresh_is_single_flight(tmp_path, vault, runner):
    index = DirectoryIndex(tmp_path / "directory.db", "example.com")

    class Connector:
        domain = "example.com"

        def __init__(self):
            self.calls = 0

        async def refresh_directory_users(self, target):
            self.calls += 1
            await asyncio.sleep(0.02)
            return target.replace_users([_user(1)])

    connector = Connector()
    state = AppState(
        vault=vault,
        runner=runner,
        audit_domain="example.com",
        connector=connector,
        token="t",
        directory_index=index,
    )
    first, second = await asyncio.gather(
        state.directory_users(),
        state.directory_users(),
    )
    assert connector.calls == 1
    assert first.total == 1 and second.total == 1


async def test_stale_snapshot_is_served_while_one_refresh_runs(tmp_path, vault, runner):
    index = DirectoryIndex(tmp_path / "directory.db", "example.com")
    index.replace_users([_user(1)])
    index.mark_stale("users")
    started = asyncio.Event()
    release = asyncio.Event()

    class Connector:
        domain = "example.com"

        async def refresh_directory_users(self, target):
            started.set()
            await release.wait()
            return target.replace_users([_user(2)])

    state = AppState(
        vault=vault,
        runner=runner,
        audit_domain="example.com",
        connector=Connector(),
        token="t",
        directory_index=index,
    )
    stale_page = await state.directory_users()
    assert stale_page.items[0].primary_email == "user00001@example.com"
    assert stale_page.refreshing is True
    await asyncio.wait_for(started.wait(), timeout=1)
    release.set()
    await asyncio.wait_for(state._directory_refresh_tasks["users"], timeout=1)
    fresh_page = await state.directory_users()
    assert fresh_page.items[0].primary_email == "user00002@example.com"
    assert fresh_page.refreshing is False
