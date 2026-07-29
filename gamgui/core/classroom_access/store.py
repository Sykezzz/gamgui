"""Owner-only persistence for entitlement policies and restart-safe plans."""

from __future__ import annotations

import json
import os
import secrets
import sqlite3
import time
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from typing import Optional, Sequence

from ..classroom.manifests import _prepare_private_database, _secure_private_file
from ..paths import app_data_dir
from ..processes import current_process_identity, process_lease_is_dead
from .models import EntitlementPlan, EntitlementPolicy, PolicyStatus


def default_entitlement_store_path() -> Path:
    return app_data_dir() / "classroom_teacher_entitlements.db"


class EntitlementStore:
    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = Path(path) if path is not None else default_entitlement_store_path()
        _prepare_private_database(self.path)
        self._init()
        self._restrict_perms()

    def _conn(self) -> sqlite3.Connection:
        _prepare_private_database(self.path)
        conn = sqlite3.connect(str(self.path), timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=10000")
        conn.execute("PRAGMA foreign_keys=ON")
        self._restrict_perms()
        return conn

    def _restrict_perms(self) -> None:
        _secure_private_file(self.path)
        for suffix in ("-wal", "-shm"):
            companion = Path(str(self.path) + suffix)
            try:
                _secure_private_file(companion)
            except FileNotFoundError:
                pass

    def _init(self) -> None:
        with closing(self._conn()) as conn, conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS entitlement_policies (
                    id TEXT PRIMARY KEY,
                    domain TEXT NOT NULL,
                    target_group TEXT NOT NULL,
                    source_mode TEXT NOT NULL,
                    source_group TEXT NOT NULL,
                    csv_mode TEXT NOT NULL,
                    csv_emails_json TEXT NOT NULL,
                    watch_path TEXT NOT NULL,
                    exception_users_json TEXT NOT NULL,
                    exception_groups_json TEXT NOT NULL,
                    connector_identity TEXT NOT NULL,
                    status TEXT NOT NULL,
                    schedule_enabled INTEGER NOT NULL,
                    schedule_hour INTEGER NOT NULL,
                    schedule_minute INTEGER NOT NULL,
                    approved_config_hash TEXT NOT NULL,
                    approved_source_hash TEXT NOT NULL,
                    pending_plan_id TEXT NOT NULL,
                    last_run_status TEXT NOT NULL,
                    last_run_message TEXT NOT NULL,
                    last_run_at REAL NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS entitlement_policy_domain
                ON entitlement_policies(domain)
                """
            )
            policy_columns = {
                str(row["name"])
                for row in conn.execute(
                    "PRAGMA table_info(entitlement_policies)"
                ).fetchall()
            }
            if "connector_identity" not in policy_columns:
                conn.execute(
                    """
                    ALTER TABLE entitlement_policies
                    ADD COLUMN connector_identity TEXT NOT NULL DEFAULT ''
                    """
                )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS entitlement_plans (
                    id TEXT PRIMARY KEY,
                    policy_id TEXT NOT NULL,
                    domain TEXT NOT NULL,
                    target_group TEXT NOT NULL,
                    desired_json TEXT NOT NULL,
                    source_emails_json TEXT NOT NULL,
                    current_json TEXT NOT NULL,
                    adds_json TEXT NOT NULL,
                    removes_json TEXT NOT NULL,
                    unchanged_json TEXT NOT NULL,
                    source_hash TEXT NOT NULL,
                    basis_hash TEXT NOT NULL,
                    status TEXT NOT NULL,
                    hold_reason TEXT NOT NULL,
                    error TEXT NOT NULL,
                    residual_json TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    run_owner TEXT NOT NULL,
                    run_pid INTEGER NOT NULL,
                    run_identity TEXT NOT NULL,
                    FOREIGN KEY (policy_id) REFERENCES entitlement_policies(id)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS entitlement_targets (
                    plan_id TEXT NOT NULL,
                    email TEXT NOT NULL,
                    action TEXT NOT NULL,
                    status TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    PRIMARY KEY (plan_id, email, action),
                    FOREIGN KEY (plan_id) REFERENCES entitlement_plans(id) ON DELETE CASCADE
                )
                """
            )
            rows = conn.execute(
                """
                SELECT id, run_pid, run_identity
                FROM entitlement_plans WHERE status = 'running'
                """
            ).fetchall()
            for row in rows:
                if process_lease_is_dead(
                    int(row["run_pid"] or 0), str(row["run_identity"] or "")
                ):
                    conn.execute(
                        """
                        UPDATE entitlement_plans
                        SET status = 'interrupted',
                            error = 'The background agent stopped before reconciliation finished.',
                            run_owner = '', run_pid = 0, run_identity = '',
                            updated_at = ?
                        WHERE id = ? AND status = 'running'
                        """,
                        (time.time(), row["id"]),
                    )

    def policy_for_domain(self, domain: str) -> Optional[EntitlementPolicy]:
        with closing(self._conn()) as conn:
            row = conn.execute(
                "SELECT * FROM entitlement_policies WHERE domain = ?",
                (domain.strip().casefold(),),
            ).fetchone()
        return self._policy(row) if row is not None else None

    def get_policy(self, policy_id: str) -> Optional[EntitlementPolicy]:
        with closing(self._conn()) as conn:
            row = conn.execute(
                "SELECT * FROM entitlement_policies WHERE id = ?", (policy_id,)
            ).fetchone()
        return self._policy(row) if row is not None else None

    def save_policy(self, policy: EntitlementPolicy) -> EntitlementPolicy:
        existing = self.get_policy(policy.id) if policy.id else None
        now = time.time()
        policy_id = policy.id or secrets.token_urlsafe(12)
        created_at = existing.created_at if existing else now
        candidate = replace(
            policy,
            id=policy_id,
            domain=policy.domain.strip().casefold(),
            target_group=policy.target_group.strip().casefold(),
            source_group=policy.source_group.strip().casefold(),
            created_at=created_at,
            updated_at=now,
        )
        prior_hash = existing.configuration_hash if existing else ""
        changed = not existing or candidate.configuration_hash != prior_hash
        if changed:
            candidate = replace(
                candidate,
                status=PolicyStatus.DRAFT.value,
                approved_config_hash="",
                pending_plan_id="",
            )
        values = self._policy_values(candidate)
        with closing(self._conn()) as conn, conn:
            conn.execute(
                """
                INSERT INTO entitlement_policies (
                    id, domain, target_group, source_mode, source_group,
                    csv_mode, csv_emails_json, watch_path,
                    exception_users_json, exception_groups_json,
                    connector_identity, status,
                    schedule_enabled, schedule_hour, schedule_minute,
                    approved_config_hash, approved_source_hash,
                    pending_plan_id, last_run_status, last_run_message,
                    last_run_at, created_at, updated_at
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
                ON CONFLICT(id) DO UPDATE SET
                    domain=excluded.domain,
                    target_group=excluded.target_group,
                    source_mode=excluded.source_mode,
                    source_group=excluded.source_group,
                    csv_mode=excluded.csv_mode,
                    csv_emails_json=excluded.csv_emails_json,
                    watch_path=excluded.watch_path,
                    exception_users_json=excluded.exception_users_json,
                    exception_groups_json=excluded.exception_groups_json,
                    connector_identity=excluded.connector_identity,
                    status=excluded.status,
                    schedule_enabled=excluded.schedule_enabled,
                    schedule_hour=excluded.schedule_hour,
                    schedule_minute=excluded.schedule_minute,
                    approved_config_hash=excluded.approved_config_hash,
                    approved_source_hash=excluded.approved_source_hash,
                    pending_plan_id=excluded.pending_plan_id,
                    last_run_status=excluded.last_run_status,
                    last_run_message=excluded.last_run_message,
                    last_run_at=excluded.last_run_at,
                    updated_at=excluded.updated_at
                """,
                values,
            )
        self._restrict_perms()
        saved = self.get_policy(policy_id)
        if saved is None:
            raise RuntimeError("Entitlement policy was not persisted.")
        return saved

    def create_plan(self, plan: EntitlementPlan) -> EntitlementPlan:
        plan_id = plan.id or secrets.token_urlsafe(12)
        now = time.time()
        candidate = replace(plan, id=plan_id, created_at=now, updated_at=now)
        with closing(self._conn()) as conn, conn:
            conn.execute(
                """
                INSERT INTO entitlement_plans (
                    id, policy_id, domain, target_group, desired_json,
                    source_emails_json, current_json, adds_json, removes_json,
                    unchanged_json, source_hash, basis_hash, status,
                    hold_reason, error, residual_json, created_at, updated_at,
                    run_owner, run_pid, run_identity
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '', 0, '')
                """,
                (
                    candidate.id,
                    candidate.policy_id,
                    candidate.domain,
                    candidate.target_group,
                    json.dumps(candidate.desired),
                    json.dumps(candidate.source_emails),
                    json.dumps(candidate.current),
                    json.dumps(candidate.adds),
                    json.dumps(candidate.removes),
                    json.dumps(candidate.unchanged),
                    candidate.source_hash,
                    candidate.basis_hash,
                    candidate.status,
                    candidate.hold_reason,
                    candidate.error,
                    json.dumps(candidate.residual),
                    candidate.created_at,
                    candidate.updated_at,
                ),
            )
            conn.executemany(
                """
                INSERT INTO entitlement_targets
                    (plan_id, email, action, status, detail)
                VALUES (?, ?, ?, 'pending', '')
                """,
                [(candidate.id, email, "add") for email in candidate.adds]
                + [(candidate.id, email, "remove") for email in candidate.removes],
            )
            conn.execute(
                """
                UPDATE entitlement_policies
                SET pending_plan_id = ?, status = CASE
                    WHEN ? = 'held' THEN 'held' ELSE status END,
                    updated_at = ?
                WHERE id = ?
                """,
                (candidate.id, candidate.status, now, candidate.policy_id),
            )
        self._restrict_perms()
        saved = self.get_plan(plan_id)
        if saved is None:
            raise RuntimeError("Entitlement plan was not persisted.")
        return saved

    def get_plan(self, plan_id: str) -> Optional[EntitlementPlan]:
        with closing(self._conn()) as conn:
            row = conn.execute(
                "SELECT * FROM entitlement_plans WHERE id = ?", (plan_id,)
            ).fetchone()
        return self._plan(row) if row is not None else None

    def approve_plan(self, plan_id: str) -> bool:
        with closing(self._conn()) as conn, conn:
            result = conn.execute(
                """
                UPDATE entitlement_plans
                SET status = 'approved', hold_reason = '', updated_at = ?
                WHERE id = ? AND status IN ('held', 'planned')
                """,
                (time.time(), plan_id),
            )
        return result.rowcount == 1

    def claim_plan(self, plan_id: str, owner_id: str) -> bool:
        pid = os.getpid()
        with closing(self._conn()) as conn, conn:
            result = conn.execute(
                """
                UPDATE entitlement_plans
                SET status = 'running', run_owner = ?, run_pid = ?,
                    run_identity = ?, updated_at = ?
                WHERE id = ? AND status IN ('approved', 'planned')
                """,
                (
                    owner_id,
                    pid,
                    current_process_identity(),
                    time.time(),
                    plan_id,
                ),
            )
        return result.rowcount == 1

    def owns_claim(self, plan_id: str, owner_id: str) -> bool:
        with closing(self._conn()) as conn:
            row = conn.execute(
                """
                SELECT 1 FROM entitlement_plans
                WHERE id = ? AND status = 'running' AND run_owner = ?
                """,
                (plan_id, owner_id),
            ).fetchone()
        return row is not None

    def mark_target(
        self,
        plan_id: str,
        email: str,
        action: str,
        *,
        ok: bool,
        detail: str,
        owner_id: str,
    ) -> None:
        with closing(self._conn()) as conn, conn:
            result = conn.execute(
                """
                UPDATE entitlement_targets
                SET status = ?, detail = ?
                WHERE plan_id = ? AND email = ? AND action = ?
                  AND EXISTS (
                    SELECT 1 FROM entitlement_plans
                    WHERE id = ? AND status = 'running' AND run_owner = ?
                  )
                """,
                (
                    "applied" if ok else "failed",
                    detail,
                    plan_id,
                    email,
                    action,
                    plan_id,
                    owner_id,
                ),
            )
        if result.rowcount != 1:
            raise PermissionError("The entitlement plan is owned by another executor.")

    def finish_plan(
        self,
        plan_id: str,
        *,
        status: str,
        residual: Sequence[str] = (),
        error: str = "",
        owner_id: str,
    ) -> None:
        if status not in {"completed", "partial", "failed", "stale", "interrupted"}:
            raise ValueError(f"Invalid entitlement plan status: {status}")
        with closing(self._conn()) as conn, conn:
            result = conn.execute(
                """
                UPDATE entitlement_plans
                SET status = ?, residual_json = ?, error = ?,
                    run_owner = '', run_pid = 0, run_identity = '',
                    updated_at = ?
                WHERE id = ? AND status = 'running' AND run_owner = ?
                """,
                (
                    status,
                    json.dumps(list(residual)),
                    error,
                    time.time(),
                    plan_id,
                    owner_id,
                ),
            )
        if result.rowcount != 1:
            raise PermissionError("The entitlement plan is owned by another executor.")

    def record_policy_result(
        self,
        policy_id: str,
        *,
        status: str,
        message: str,
        source_hash: str = "",
        source_emails: Sequence[str] = (),
        activate: bool = False,
        preserve_pending: bool = False,
    ) -> EntitlementPolicy:
        now = time.time()
        with closing(self._conn()) as conn, conn:
            row = conn.execute(
                "SELECT * FROM entitlement_policies WHERE id = ?", (policy_id,)
            ).fetchone()
            if row is None:
                raise KeyError("Entitlement policy not found.")
            policy = self._policy(row)
            next_status = (
                PolicyStatus.ACTIVE.value
                if activate or status == "completed"
                else PolicyStatus.HELD.value
                if status in {"held", "failed", "partial", "stale", "interrupted"}
                else policy.status
            )
            csv_json = (
                json.dumps(list(source_emails))
                if source_emails
                else json.dumps(list(policy.csv_emails))
            )
            approved_config = (
                replace(policy, csv_emails=tuple(source_emails)).configuration_hash
                if activate and source_emails
                else policy.configuration_hash
                if activate
                else policy.approved_config_hash
            )
            conn.execute(
                """
                UPDATE entitlement_policies
                SET status = ?, last_run_status = ?, last_run_message = ?,
                    last_run_at = ?, approved_config_hash = ?,
                    approved_source_hash = CASE WHEN ? != '' THEN ? ELSE approved_source_hash END,
                    csv_emails_json = ?,
                    pending_plan_id = CASE WHEN ? THEN pending_plan_id ELSE '' END,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    next_status,
                    status,
                    message,
                    now,
                    approved_config,
                    source_hash,
                    source_hash,
                    csv_json,
                    int(preserve_pending),
                    now,
                    policy_id,
                ),
            )
        saved = self.get_policy(policy_id)
        if saved is None:
            raise RuntimeError("Entitlement policy result was not persisted.")
        return saved

    def disable_policy(self, policy_id: str) -> EntitlementPolicy:
        with closing(self._conn()) as conn, conn:
            conn.execute(
                """
                UPDATE entitlement_policies
                SET status = 'disabled', schedule_enabled = 0,
                    pending_plan_id = '', updated_at = ?
                WHERE id = ?
                """,
                (time.time(), policy_id),
            )
        policy = self.get_policy(policy_id)
        if policy is None:
            raise KeyError("Entitlement policy not found.")
        return policy

    def has_active_jobs(self) -> bool:
        with closing(self._conn()) as conn:
            row = conn.execute(
                "SELECT 1 FROM entitlement_plans WHERE status = 'running' LIMIT 1"
            ).fetchone()
        return row is not None

    @staticmethod
    def _policy(row: sqlite3.Row) -> EntitlementPolicy:
        return EntitlementPolicy(
            id=str(row["id"]),
            domain=str(row["domain"]),
            target_group=str(row["target_group"]),
            source_mode=str(row["source_mode"]),
            source_group=str(row["source_group"]),
            csv_mode=str(row["csv_mode"]),
            csv_emails=tuple(json.loads(row["csv_emails_json"] or "[]")),
            watch_path=str(row["watch_path"]),
            exception_users=tuple(json.loads(row["exception_users_json"] or "[]")),
            exception_groups=tuple(json.loads(row["exception_groups_json"] or "[]")),
            connector_identity=str(row["connector_identity"]),
            status=str(row["status"]),
            schedule_enabled=bool(row["schedule_enabled"]),
            schedule_hour=int(row["schedule_hour"]),
            schedule_minute=int(row["schedule_minute"]),
            approved_config_hash=str(row["approved_config_hash"]),
            approved_source_hash=str(row["approved_source_hash"]),
            pending_plan_id=str(row["pending_plan_id"]),
            last_run_status=str(row["last_run_status"]),
            last_run_message=str(row["last_run_message"]),
            last_run_at=float(row["last_run_at"]),
            created_at=float(row["created_at"]),
            updated_at=float(row["updated_at"]),
        )

    @staticmethod
    def _plan(row: sqlite3.Row) -> EntitlementPlan:
        return EntitlementPlan(
            id=str(row["id"]),
            policy_id=str(row["policy_id"]),
            domain=str(row["domain"]),
            target_group=str(row["target_group"]),
            desired=tuple(json.loads(row["desired_json"] or "[]")),
            source_emails=tuple(json.loads(row["source_emails_json"] or "[]")),
            current=tuple(json.loads(row["current_json"] or "[]")),
            adds=tuple(json.loads(row["adds_json"] or "[]")),
            removes=tuple(json.loads(row["removes_json"] or "[]")),
            unchanged=tuple(json.loads(row["unchanged_json"] or "[]")),
            source_hash=str(row["source_hash"]),
            basis_hash=str(row["basis_hash"]),
            status=str(row["status"]),
            hold_reason=str(row["hold_reason"]),
            error=str(row["error"]),
            residual=tuple(json.loads(row["residual_json"] or "[]")),
            created_at=float(row["created_at"]),
            updated_at=float(row["updated_at"]),
        )

    @staticmethod
    def _policy_values(policy: EntitlementPolicy) -> tuple:
        return (
            policy.id,
            policy.domain,
            policy.target_group,
            policy.source_mode,
            policy.source_group,
            policy.csv_mode,
            json.dumps(list(policy.csv_emails)),
            policy.watch_path,
            json.dumps(list(policy.exception_users)),
            json.dumps(list(policy.exception_groups)),
            policy.connector_identity,
            policy.status,
            int(policy.schedule_enabled),
            policy.schedule_hour,
            policy.schedule_minute,
            policy.approved_config_hash,
            policy.approved_source_hash,
            policy.pending_plan_id,
            policy.last_run_status,
            policy.last_run_message,
            policy.last_run_at,
            policy.created_at,
            policy.updated_at,
        )
