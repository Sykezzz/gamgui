from __future__ import annotations

import asyncio
import json
import os
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests.platform_fixtures import MOCK_GAM, MOCK_GAM_COMMAND_PREFIX

import gamgui.core.audit as audit_mod
from gamgui.core.audit import AUDIT_PAGE_SIZE, AuditIndex, AuditLog
from gamgui.core.connectors.gam_connector import GAMConnector
from gamgui.core.directory_index import DirectoryIndex
from gamgui.core.gam.models import GAMUser
from gamgui.core.gam.runner import GAMRunner
from gamgui.core.reports import (
    build_reports,
    indexed_report_page,
    indexed_report_summaries,
)
from gamgui.core.secrets.vault import InMemoryBackend, SecretsVault
from gamgui.web.server import AppState, create_app


DOMAIN = "example.com"
FIXTURES = Path(__file__).parent / "fixtures"


def _write_audit(path: Path, records: list[dict]) -> None:
    path.write_text(
        "".join(json.dumps(record) + "\n" for record in records),
        encoding="utf-8",
    )


def test_audit_index_parses_once_then_only_ingests_appends(tmp_path, monkeypatch):
    path = tmp_path / "audit.jsonl"
    _write_audit(
        path,
        [
            {
                "ts": str(i),
                "action": "set_vacation" if i % 2 else "suspend",
                "target": f"user{i}@example.com",
                "ok": i % 7 != 0,
            }
            for i in range(250)
        ],
    )
    calls = {"decode": 0}
    real_decode = audit_mod._decode_record

    def counting_decode(line: bytes):
        calls["decode"] += 1
        return real_decode(line)

    monkeypatch.setattr(audit_mod, "_decode_record", counting_decode)
    index = AuditIndex(path, tmp_path / "audit-index.db")

    first = index.page(q="vacation")
    assert calls["decode"] == 250
    assert len(first.rows) <= AUDIT_PAGE_SIZE
    assert first.total == 125

    calls["decode"] = 0
    assert index.page(q="VACATION").total == 125
    with pytest.raises(ValueError, match="at least 3"):
        index.page(q="va")
    assert index.page(q="user24@example.com").total == 1
    assert index.page(failed=True).total == 36
    assert calls["decode"] == 0  # searches query SQLite; they do not reparse JSONL

    with open(path, "a", encoding="utf-8") as source:
        source.write(
            json.dumps(
                {
                    "ts": "new",
                    "action": "set_vacation",
                    "target": "new@example.com",
                    "ok": False,
                }
            )
            + "\n"
        )
    assert index.page(q="new@example.com").total == 1
    assert calls["decode"] == 1


def test_audit_index_rebuilds_after_source_replacement(tmp_path):
    path = tmp_path / "audit.jsonl"
    index = AuditIndex(path, tmp_path / "audit-index.db")
    _write_audit(path, [{"ts": "1", "action": "before", "target": "old", "ok": True}])
    assert index.page(q="before").total == 1

    _write_audit(path, [{"ts": "2", "action": "after", "target": "new", "ok": False}])
    assert index.page(q="before").total == 0
    assert index.page(q="after", failed=True).total == 1


def test_audit_index_detects_same_size_middle_rewrite_with_preserved_mtime(tmp_path):
    path = tmp_path / "audit.jsonl"
    before = [
        {
            "ts": str(i),
            "action": "before" if i == 200 else "noop",
            "target": "middle-old" if i == 200 else f"user{i:03d}",
            "ok": True,
            "extra": {"padding": "x" * 80},
        }
        for i in range(401)
    ]
    _write_audit(path, before)
    original_stat = path.stat()
    index = AuditIndex(path, tmp_path / "audit-index.db")
    assert index.page(q="middle-old").total == 1

    after = [dict(record) for record in before]
    after[200]["target"] = "middle-new"  # same byte length, outside first/last samples
    _write_audit(path, after)
    os.utime(path, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))

    assert index.page(q="middle-old").total == 0
    assert index.page(q="middle-new").total == 1


def test_audit_export_iterator_does_not_hold_sync_lock_between_yields(tmp_path):
    path = tmp_path / "audit.jsonl"
    _write_audit(
        path,
        [
            {"ts": str(i), "action": "noop", "target": f"user{i}", "ok": True}
            for i in range(10)
        ],
    )
    index = AuditIndex(path, tmp_path / "audit-index.db")
    records = index.iter_filtered()
    assert next(records)["target"] == "user9"

    def can_take_lock() -> bool:
        acquired = index._lock.acquire(blocking=False)
        if acquired:
            index._lock.release()
        return acquired

    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            assert pool.submit(can_take_lock).result(timeout=1)
    finally:
        records.close()


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits are not enforced on Windows")
def test_audit_index_files_are_owner_only(tmp_path):
    path = tmp_path / "audit.jsonl"
    _write_audit(path, [{"ts": "1", "action": "noop", "ok": True}])
    index = AuditIndex(path, tmp_path / "audit-index.db")
    index.page()
    assert (index.index_path.stat().st_mode & 0o777) == 0o600
    for suffix in ("-wal", "-shm"):
        sidecar = Path(str(index.index_path) + suffix)
        if sidecar.exists():
            assert (sidecar.stat().st_mode & 0o777) == 0o600


def _indexed_users(count: int = 125) -> list[GAMUser]:
    return [
        GAMUser(
            primary_email=f"user{i:03d}@example.com",
            given_name=f"User {i:03d}",
            family_name="District",
            suspended=i % 17 == 0,
            is_admin=i % 31 == 0,
            enrolled_2sv=i % 3 == 0,
            title="" if i % 5 else "Teacher",
            department="" if i % 7 else "Instruction",
            last_login_time=(
                "2024-01-01T00:00:00Z"
                if i % 4
                else "2026-07-21T00:00:00Z"
            ),
        )
        for i in range(count)
    ]


def test_indexed_reports_are_count_first_and_hard_bounded(tmp_path):
    index = DirectoryIndex(tmp_path / "directory.db", DOMAIN)
    users = _indexed_users()
    users.append(
        GAMUser(
            primary_email="malformed-login@example.com",
            enrolled_2sv=True,
            title="Teacher",
            department="Instruction",
            last_login_time="not-a-date",
        )
    )
    index.replace_users(users)
    now = datetime(2026, 7, 22, tzinfo=timezone.utc)
    summaries = {
        report.key: report
        for report in indexed_report_summaries(index, now=now)
    }
    assert {"no_2sv", "inactive", "admins", "suspended", "no_title", "no_department"} == set(
        summaries
    )
    assert {"no_recovery", "no_phone", "no_location"}.isdisjoint(summaries)
    legacy_inactive = {
        report.key: report.count for report in build_reports(users, now=now)
    }["inactive"]
    assert summaries["inactive"].count == legacy_inactive

    first = indexed_report_page(index, "no_2sv", limit=500)
    assert len(first.items) <= 50
    assert first.total == summaries["no_2sv"].count
    assert first.next_cursor
    second = indexed_report_page(index, "no_2sv", cursor=first.next_cursor)
    assert len(second.items) <= 50
    assert {user.primary_email for user in first.items}.isdisjoint(
        user.primary_email for user in second.items
    )
    with pytest.raises(ValueError, match="cursor"):
        indexed_report_page(index, "admins", cursor=first.next_cursor)


def test_report_cursor_is_rejected_after_directory_snapshot_refresh(tmp_path):
    index = DirectoryIndex(tmp_path / "directory.db", DOMAIN)
    users = _indexed_users()
    index.replace_users(users)
    first = indexed_report_page(index, "no_2sv", limit=10)
    assert first.next_cursor

    # A completed replacement is a new snapshot even when its rows happen to be identical.
    prior_generation = index.status().users_updated_at
    with sqlite3.connect(index.path) as conn:
        conn.execute(
            "UPDATE snapshots SET updated_at = ? WHERE domain = ? AND kind = 'users'",
            (float(prior_generation or 0) + 1.0, DOMAIN),
        )

    with pytest.raises(ValueError, match="cursor"):
        indexed_report_page(
            index,
            "no_2sv",
            limit=10,
            cursor=first.next_cursor,
        )


@pytest.fixture
def indexed_client(tmp_path, monkeypatch):
    monkeypatch.setenv("GAM_MOCK_FIXTURES", str(FIXTURES))
    vault = SecretsVault(InMemoryBackend())
    vault.set_all(
        DOMAIN,
        {
            "client_secrets": "{}",
            "oauth2": "tok",
            "oauth2service": '{"client_id": "x"}',
        },
    )
    runner = GAMRunner(
        vault=vault, gam_binary=MOCK_GAM, base_dir=tmp_path,
        command_prefix=MOCK_GAM_COMMAND_PREFIX,
    )
    connector = GAMConnector(
        runner=runner,
        domain=DOMAIN,
        audit=AuditLog(tmp_path / "audit.jsonl"),
    )
    directory_index = DirectoryIndex(tmp_path / "directory.db", DOMAIN)
    directory_index.replace_users(_indexed_users())
    state = AppState(
        vault=vault,
        runner=runner,
        audit_domain=DOMAIN,
        connector=connector,
        token="t",
        directory_index=directory_index,
    )
    client = TestClient(create_app(state))
    client.get("/?token=t")
    return client


def test_reports_route_sends_counts_then_one_bounded_bucket(indexed_client):
    response = indexed_client.get("/reports")
    assert response.status_code == 200
    assert "No 2-step verification" in response.text
    assert "user001@example.com" not in response.text
    assert "hx-trigger=\"load\"" not in response.text
    assert "Recovery email, phone, and location stay live" in response.text

    bucket = indexed_client.get("/reports/bucket", params={"key": "no_2sv"})
    assert bucket.status_code == 200
    assert bucket.text.count("@example.com") <= 50
    assert len(bucket.content) < 100_000


def test_report_response_stays_under_100kb_with_hostile_summary_fields(indexed_client):
    index = indexed_client.app.state.gamgui.directory_index
    index.replace_users(
        [
            GAMUser(
                primary_email=f"hostile{i:03d}@example.com",
                given_name="G" * 20_000,
                family_name="F" * 20_000,
                org_unit_path="/" + ("O" * 20_000),
                enrolled_2sv=False,
            )
            for i in range(60)
        ]
    )
    bucket = indexed_client.get("/reports/bucket", params={"key": "no_2sv"})
    assert bucket.status_code == 200
    assert bucket.text.count("@example.com") <= 50
    assert len(bucket.content) < 100_000
    assert "G" * 200 not in bucket.text


def test_audit_route_is_bounded_and_replaces_stale_search(indexed_client):
    audit_path = indexed_client.app.state.gamgui.connector.audit.path
    _write_audit(
        audit_path,
        [
            {
                "ts": str(i),
                "action": "suspend",
                "target": f"audit{i:03d}@example.com",
                "ok": i % 2 == 0,
            }
            for i in range(80)
        ],
    )
    page = indexed_client.get("/audit")
    assert page.status_code == 200
    assert 'delay:300ms' in page.text
    assert 'hx-sync="this:replace"' in page.text
    assert page.text.count("@example.com") == AUDIT_PAGE_SIZE

    filtered = indexed_client.get("/audit/rows", params={"q": "audit079"})
    assert filtered.status_code == 200
    assert "audit079@example.com" in filtered.text
    assert "audit078@example.com" not in filtered.text


def test_short_audit_query_is_rejected_before_index_sync_or_scan(
    indexed_client,
    monkeypatch,
):
    import gamgui.web.routes.audit as audit_routes

    page = indexed_client.get("/audit")
    assert page.status_code == 200
    assert 'minlength="3"' in page.text
    assert 'maxlength="200"' in page.text
    assert 'hx-validate="true"' in page.text
    assert "Shorter searches are rejected" in page.text

    def forbidden_index(*args, **kwargs):
        raise AssertionError("short audit query reached index initialization")

    monkeypatch.setattr(audit_routes, "get_audit_index", forbidden_index)
    response = indexed_client.get("/audit/rows", params={"q": "ab"})
    export = indexed_client.get("/audit/export.csv", params={"q": "x"})

    assert response.status_code == 400
    assert 'role="alert"' in response.text
    assert "at least 3 characters" in response.text
    assert export.status_code == 400
    assert "at least 3 characters" in export.text


def test_audit_hostile_records_have_bounded_index_and_response_payloads(
    indexed_client,
):
    audit_path = indexed_client.app.state.gamgui.connector.audit.path
    huge = "X" * 50_000
    _write_audit(
        audit_path,
        [
            {
                "ts": huge,
                "connector": huge,
                "action": huge,
                "target": f"hostile{i:02d}@example.com{huge}",
                "argv": [huge] * 100,
                "exit_code": huge,
                "ok": False,
                "actor": huge,
                "extra": {
                    "error": huge,
                    **{f"field{j}": huge for j in range(40)},
                },
            }
            for i in range(30)
        ],
    )

    response = indexed_client.get("/audit")
    assert response.status_code == 200
    assert len(response.content) < 100_000
    assert huge[:1_000] not in response.text
    assert response.text.count('<tr class="border-b') == AUDIT_PAGE_SIZE

    index_path = audit_path.with_name(audit_path.name + ".index.sqlite3")
    with sqlite3.connect(index_path) as conn:
        max_payload = int(
            conn.execute("SELECT MAX(length(payload)) FROM records").fetchone()[0]
        )
        max_haystack = int(
            conn.execute("SELECT MAX(length(haystack)) FROM records").fetchone()[0]
        )
    assert max_payload < 20_000
    assert max_haystack < 10_000


def test_audit_index_initialization_runs_off_the_request_loop(indexed_client, monkeypatch):
    import gamgui.web.routes.audit as audit_routes

    real_get = audit_routes.get_audit_index

    def guarded_get(path):
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass
        else:
            raise AssertionError("audit index initialized on the request event loop")
        return real_get(path)

    monkeypatch.setattr(audit_routes, "get_audit_index", guarded_get)
    response = indexed_client.get("/audit")
    assert response.status_code == 200
