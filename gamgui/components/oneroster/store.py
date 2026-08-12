"""Protected persistent state for OneRoster imports, manifests, and safety controls."""

from __future__ import annotations

import base64
import csv
import hashlib
import io
import json
import os
import secrets
import shutil
import sqlite3
import stat
import time
from contextlib import closing
from dataclasses import replace
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, BinaryIO, Iterable, Mapping, Optional, Sequence, TextIO

from gamgui.core.paths import app_data_dir
from gamgui.core.processes import current_process_identity, process_lease_is_dead

from .gate import arm_gate as gate_arm
from .gate import closed_gate, hold_gate as gate_hold, open_gate as gate_open
from .ingest import (
    district_roster_date,
    normalize_course_name_template,
    read_schedule_scope,
    rebuild_course_plans,
)
from .models import (
    AUTOMATIC_SESSION_SCOPE,
    ClassroomImportManifest,
    DEFAULT_COURSE_NAME_TEMPLATE,
    DashboardStatus,
    ExecutionBatch,
    ExecutionBatchProgress,
    ExecutionPhaseProgress,
    ExecutionProgress,
    ExecutionRun,
    GateState,
    ImportAction,
    ImportIssue,
    IssueSeverity,
    ManifestPage,
    MAX_PAGE_SIZE,
    OneRosterError,
    OneRosterSnapshot,
    PreviewPage,
    PurgePreview,
    ScopeReadiness,
    SnapshotCounts,
    SnapshotState,
    StudentEnrollmentGate,
    ThresholdEvaluation,
    ThresholdDenial,
    ThresholdOverride,
    ThresholdProfile,
    canonical_hash,
)
from .preview_index import (
    ensure_preview_index,
    preview_index_ready,
    preview_keys,
    preview_total,
    source_rows,
)
from .thresholds import evaluation_hash


def default_component_data_root() -> Path:
    return app_data_dir() / "components" / "classroom-oneroster"


_PROGRESS_PHASES = (
    (
        "classes",
        "Class setup",
        frozenset({
            "course_create",
            "course_update",
            "course_activate",
            "course_archive",
        }),
    ),
    (
        "teachers",
        "Teacher access",
        frozenset({"teacher_add", "teacher_remove", "owner_transfer"}),
    ),
    (
        "students",
        "Student roster",
        frozenset({"student_add", "student_remove"}),
    ),
)


def _progress_phase(kind: str) -> tuple[str, str]:
    normalized = str(kind or "").casefold()
    for key, label, kinds in _PROGRESS_PHASES:
        if normalized in kinds:
            return key, label
    return "other", "Other checked work"


class OneRosterStore:
    """Domain-scoped facade over component state and per-import normalized databases."""

    def __init__(self, domain: str, root: Optional[Path] = None) -> None:
        self.domain = _normalize_domain(domain)
        self.root = Path(root) if root is not None else default_component_data_root()
        self.snapshots_root = self.root / "snapshots"
        self.state_path = self.root / "state.db"
        _secure_private_directory(self.root, create=True)
        _secure_private_directory(self.snapshots_root, create=True)
        _prepare_private_database(self.state_path)
        self._init_state()
        self.recover_interrupted()
        self._restrict_state_perms()

    def new_import(self, filename: str, *, now: Optional[float] = None) -> tuple[str, Path]:
        imported_at = float(now if now is not None else time.time())
        import_id = secrets.token_hex(16)
        snapshot_dir = self.snapshots_root / import_id
        snapshot_dir.mkdir(mode=0o700)
        _chmod(snapshot_dir, 0o700)
        with closing(self._conn()) as conn, conn:
            conn.execute(
                """
                INSERT INTO imports (
                    id, domain, filename, source_sha256, state, package_mode,
                    selected_session_id, imported_at, expires_at, counts_json,
                    issue_count, blocking_issue_count, accepted_at,
                    course_name_template
                ) VALUES (
                    ?, ?, ?, '', 'preparing', 'invalid', '', ?, ?, '{}',
                    0, 0, 0, ?
                )
                """,
                (
                    import_id,
                    self.domain,
                    Path(filename or "OneRoster.zip").name,
                    imported_at,
                    imported_at + (30 * 24 * 60 * 60),
                    DEFAULT_COURSE_NAME_TEMPLATE,
                ),
            )
        self._restrict_state_perms()
        return import_id, snapshot_dir

    def discard_import(self, import_id: str) -> None:
        snapshot_dir = self.snapshot_dir(import_id)
        _remove_tree(snapshot_dir, self.snapshots_root)
        with closing(self._conn()) as conn, conn:
            conn.execute(
                "DELETE FROM imports WHERE domain = ? AND id = ?",
                (self.domain, import_id),
            )

    def complete_import(
        self,
        import_id: str,
        *,
        source_sha256: str,
        state: SnapshotState,
        package_mode: str,
        selected_session_id: str,
        counts: SnapshotCounts,
        issue_count: int,
        blocking_issue_count: int,
    ) -> OneRosterSnapshot:
        scope = read_schedule_scope(
            self.normalized_path(import_id),
            self.domain,
        )
        with closing(self._conn()) as conn, conn:
            result = conn.execute(
                """
                UPDATE imports
                SET source_sha256 = ?, state = ?, package_mode = ?,
                    selected_session_id = ?, counts_json = ?, issue_count = ?,
                    blocking_issue_count = ?, scope_date = ?,
                    initial_scope_date = CASE
                        WHEN initial_scope_date = '' THEN ?
                        ELSE initial_scope_date
                    END,
                    school_year_id = ?, school_year_title = ?
                WHERE domain = ? AND id = ?
                """,
                (
                    source_sha256,
                    state.value,
                    package_mode,
                    selected_session_id,
                    json.dumps(counts.to_dict(), sort_keys=True),
                    int(issue_count),
                    int(blocking_issue_count),
                    scope["scope_date"],
                    scope["scope_date"],
                    scope["school_year_id"],
                    scope["school_year_title"],
                    self.domain,
                    import_id,
                ),
            )
            if result.rowcount != 1:
                raise KeyError("OneRoster import not found.")
        self._restrict_state_perms()
        return self.get_import(import_id)

    def get_import(self, import_id: str) -> OneRosterSnapshot:
        _validate_id(import_id)
        with closing(self._conn()) as conn:
            row = conn.execute(
                "SELECT * FROM imports WHERE domain = ? AND id = ?",
                (self.domain, import_id),
            ).fetchone()
        if row is None:
            raise KeyError("OneRoster import not found.")
        snapshot = _snapshot_from_row(row)
        if (
            snapshot.state not in {SnapshotState.PREPARING, SnapshotState.EXPIRED}
            and not self.normalized_path(snapshot.id).is_file()
        ):
            with closing(self._conn()) as conn, conn:
                conn.execute(
                    "UPDATE imports SET state = 'expired' WHERE domain = ? AND id = ?",
                    (self.domain, snapshot.id),
                )
            return OneRosterSnapshot(
                id=snapshot.id,
                domain=snapshot.domain,
                filename=snapshot.filename,
                source_sha256=snapshot.source_sha256,
                state=SnapshotState.EXPIRED,
                package_mode=snapshot.package_mode,
                selected_session_id=snapshot.selected_session_id,
                imported_at=snapshot.imported_at,
                expires_at=snapshot.expires_at,
                counts=snapshot.counts,
                issue_count=snapshot.issue_count,
                blocking_issue_count=snapshot.blocking_issue_count,
                course_name_template=snapshot.course_name_template,
                scope_date=snapshot.scope_date,
                initial_scope_date=snapshot.initial_scope_date,
                school_year_id=snapshot.school_year_id,
                school_year_title=snapshot.school_year_title,
            )
        if (
            snapshot.package_mode == "bulk"
            and snapshot.state not in {SnapshotState.PREPARING, SnapshotState.EXPIRED}
            and (
                snapshot.selected_session_id != AUTOMATIC_SESSION_SCOPE
                or not snapshot.scope_date
            )
        ):
            return self._refresh_automatic_scope(snapshot)
        return snapshot

    def _refresh_automatic_scope(
        self,
        snapshot: OneRosterSnapshot,
        *,
        today: Optional[date] = None,
    ) -> OneRosterSnapshot:
        counts, issues = rebuild_course_plans(
            self.normalized_path(snapshot.id),
            domain=self.domain,
            selected_session_id=AUTOMATIC_SESSION_SCOPE,
            course_name_template=snapshot.course_name_template,
            today=today,
        )
        blocking = sum(issue.blocking for issue in issues)
        state = (
            SnapshotState.READY
            if snapshot.package_mode == "bulk" and blocking == 0
            else SnapshotState.BLOCKED
        )
        return self.complete_import(
            snapshot.id,
            source_sha256=snapshot.source_sha256,
            state=state,
            package_mode=snapshot.package_mode,
            selected_session_id=AUTOMATIC_SESSION_SCOPE,
            counts=counts,
            issue_count=len(issues),
            blocking_issue_count=blocking,
        )

    def refresh_schedule_scope(
        self,
        import_id: str,
        *,
        today: Optional[date] = None,
    ) -> OneRosterSnapshot:
        snapshot = self.get_import(import_id)
        self._require_material(snapshot)
        scope_date = today or district_roster_date()
        if (
            snapshot.selected_session_id == AUTOMATIC_SESSION_SCOPE
            and snapshot.scope_date == scope_date.isoformat()
        ):
            return snapshot
        return self._refresh_automatic_scope(snapshot, today=scope_date)

    def history(self, limit: int = MAX_PAGE_SIZE) -> tuple[OneRosterSnapshot, ...]:
        page_size = max(1, min(int(limit or MAX_PAGE_SIZE), MAX_PAGE_SIZE))
        with closing(self._conn()) as conn, conn:
            rows = conn.execute(
                """
                SELECT * FROM imports WHERE domain = ?
                ORDER BY imported_at DESC, id DESC LIMIT ?
                """,
                (self.domain, page_size),
            ).fetchall()
            missing_material = [
                str(row["id"])
                for row in rows
                if str(row["state"]) not in {
                    SnapshotState.PREPARING.value,
                    SnapshotState.EXPIRED.value,
                }
                and not self.normalized_path(str(row["id"])).is_file()
            ]
            if missing_material:
                conn.executemany(
                    """
                    UPDATE imports SET state = 'expired'
                    WHERE domain = ? AND id = ?
                    """,
                    [(self.domain, import_id) for import_id in missing_material],
                )
                rows = conn.execute(
                    """
                    SELECT * FROM imports WHERE domain = ?
                    ORDER BY imported_at DESC, id DESC LIMIT ?
                    """,
                    (self.domain, page_size),
                ).fetchall()
        return tuple(_snapshot_from_row(row) for row in rows)

    def select_session(self, import_id: str, session_id: str) -> OneRosterSnapshot:
        """Compatibility shim: roster scope is now derived automatically."""

        del session_id
        return self.refresh_schedule_scope(import_id)

    def configure_course_naming(
        self,
        import_id: str,
        template: str,
    ) -> OneRosterSnapshot:
        snapshot = self.get_import(import_id)
        self._require_material(snapshot)
        normalized_template = normalize_course_name_template(template)
        counts, issues = rebuild_course_plans(
            self.normalized_path(import_id),
            domain=self.domain,
            selected_session_id=snapshot.selected_session_id,
            course_name_template=normalized_template,
        )
        blocking = sum(issue.blocking for issue in issues)
        state = (
            SnapshotState.READY
            if snapshot.package_mode == "bulk" and blocking == 0
            else SnapshotState.BLOCKED
        )
        scope = read_schedule_scope(self.normalized_path(import_id), self.domain)
        with closing(self._conn()) as conn, conn:
            result = conn.execute(
                """
                UPDATE imports
                SET course_name_template = ?, state = ?, counts_json = ?,
                    issue_count = ?, blocking_issue_count = ?, scope_date = ?,
                    school_year_id = ?, school_year_title = ?
                WHERE domain = ? AND id = ?
                """,
                (
                    normalized_template,
                    state.value,
                    json.dumps(counts.to_dict(), sort_keys=True),
                    len(issues),
                    blocking,
                    scope["scope_date"],
                    scope["school_year_id"],
                    scope["school_year_title"],
                    self.domain,
                    import_id,
                ),
            )
            if result.rowcount != 1:
                raise KeyError("OneRoster import not found.")
        self._restrict_state_perms()
        return self.get_import(import_id)

    def preview(
        self,
        import_id: str,
        kind: str,
        query: str = "",
        cursor: Optional[str] = None,
        limit: int = MAX_PAGE_SIZE,
    ) -> PreviewPage:
        snapshot = self.refresh_schedule_scope(import_id)
        self._require_material(snapshot)
        normalized = self.normalized_path(import_id)
        page_size = max(1, min(int(limit or MAX_PAGE_SIZE), MAX_PAGE_SIZE))
        preview_kind = (kind or "").strip().casefold()
        q = (query or "").strip()
        after_rowid = _decode_cursor(cursor, import_id, preview_kind, q)
        with closing(_snapshot_conn(normalized)) as probe:
            indexed = preview_index_ready(probe)
        if not indexed:
            with closing(_snapshot_write_conn(normalized)) as writer:
                ensure_preview_index(writer, self.domain)
            _chmod(normalized, 0o600)
        with closing(_snapshot_conn(normalized)) as conn:
            hits = preview_keys(
                conn,
                preview_kind,
                q,
                after_rowid=after_rowid,
                limit=page_size + 1,
            )
            page_hits = hits[:page_size]
            rows = source_rows(
                conn,
                preview_kind,
                self.domain,
                [entity_key for _, entity_key in page_hits],
            )
            total, total_exact = preview_total(conn, preview_kind, q)
        items = tuple(_preview_item(preview_kind, row) for row in rows)
        next_cursor = (
            _encode_cursor(page_hits[-1][0], import_id, preview_kind, q)
            if len(hits) > page_size and page_hits
            else None
        )
        return PreviewPage(
            items=items,
            next_cursor=next_cursor,
            total=total,
            limit=page_size,
            total_exact=total_exact,
        )

    def schedule_scope(self, import_id: str) -> Mapping[str, str]:
        snapshot = self.get_import(import_id)
        self._require_material(snapshot)
        return read_schedule_scope(self.normalized_path(import_id), self.domain)

    def write_export(
        self,
        import_id: str,
        kind: str,
        stream: BinaryIO | TextIO,
    ) -> int:
        snapshot = self.get_import(import_id)
        self._require_material(snapshot)
        export_kind = (kind or "").strip().casefold().removesuffix(".csv")
        config = _export_query(export_kind)
        if config is None:
            raise ValueError("Unknown OneRoster GAM export.")
        fields, sql = config
        count = 0
        _write_text(stream, _csv_line(fields))
        with closing(_snapshot_conn(self.normalized_path(import_id))) as conn:
            for row in conn.execute(sql, (self.domain,)):
                _write_text(stream, _csv_line([str(row[field] or "") for field in fields]))
                count += 1
        return count

    def get_threshold_profile(self) -> ThresholdProfile:
        with closing(self._conn()) as conn:
            row = conn.execute(
                "SELECT profile_json FROM threshold_profiles WHERE domain = ?",
                (self.domain,),
            ).fetchone()
        if row is None:
            return ThresholdProfile()
        return ThresholdProfile.from_mapping(json.loads(str(row["profile_json"])))

    def save_threshold_profile(self, profile: ThresholdProfile) -> ThresholdProfile:
        payload = json.dumps(profile.to_dict(), sort_keys=True, separators=(",", ":"))
        with closing(self._conn()) as conn, conn:
            conn.execute(
                """
                INSERT INTO threshold_profiles(domain, profile_json, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(domain) DO UPDATE SET
                    profile_json = excluded.profile_json,
                    updated_at = excluded.updated_at
                """,
                (self.domain, payload, time.time()),
            )
        self._restrict_state_perms()
        return profile

    def baseline_counts(self, import_id: str) -> dict[str, int]:
        snapshot = self.get_import(import_id)
        with closing(self._conn()) as conn:
            row = conn.execute(
                """
                SELECT counts_json FROM imports
                WHERE domain = ? AND accepted_at > 0 AND imported_at < ?
                ORDER BY accepted_at DESC LIMIT 1
                """,
                (self.domain, snapshot.imported_at),
            ).fetchone()
        prior = SnapshotCounts.from_mapping(json.loads(row[0])) if row else SnapshotCounts()
        return {
            "course_create": prior.ready_courses,
            "course_update": prior.ready_courses,
            "course_archive": prior.ready_courses,
            "teacher_add": prior.teachers,
            "teacher_remove": prior.teachers,
            "student_add": prior.students,
            "student_remove": prior.students,
            "owner_mismatch": prior.ready_courses,
            "record_rejected": prior.classes,
        }

    def mark_accepted(self, import_id: str, *, when: Optional[float] = None) -> None:
        snapshot = self.get_import(import_id)
        if not snapshot.ready_for_apply:
            raise OneRosterError(
                "OR-IMPORT-BLOCKED",
                "A blocked or preview-only import cannot become the accepted baseline.",
            )
        aliases = _managed_aliases_from_snapshot(
            self.normalized_path(import_id),
            self.domain,
        )
        accepted_at = float(when if when is not None else time.time())
        with closing(self._conn()) as conn, conn:
            conn.execute(
                """
                DELETE FROM accepted_managed_aliases
                WHERE domain = ? AND import_id = ?
                """,
                (self.domain, import_id),
            )
            conn.executemany(
                """
                INSERT INTO accepted_managed_aliases (
                    domain, import_id, alias, accepted_at
                ) VALUES (?, ?, ?, ?)
                """,
                [
                    (self.domain, import_id, alias, accepted_at)
                    for alias in aliases
                ],
            )
            conn.execute(
                "UPDATE imports SET accepted_at = ? WHERE domain = ? AND id = ?",
                (accepted_at, self.domain, import_id),
            )

    def maybe_mark_import_accepted(
        self,
        manifest_id: str,
        *,
        when: Optional[float] = None,
    ) -> bool:
        """Advance the full-snapshot baseline only after its latest plan set finishes.

        Limited Imports are deliberately never accepted as a new full baseline: doing
        so would forget removals and archives that the additions-only run omitted.
        Separate archive and ownership plans sharing the exact live evidence must also
        complete before the snapshot becomes authoritative.
        """

        manifest = self.get_manifest_page(manifest_id, limit=1).manifest
        if manifest.plan_kind == "limited":
            return False
        with closing(self._conn()) as conn:
            rows = conn.execute(
                """
                SELECT id, plan_kind, status, created_at
                FROM manifests
                WHERE domain = ? AND import_id = ? AND source_hash = ?
                  AND config_hash = ? AND live_hash = ?
                  AND threshold_evaluation_hash = ?
                ORDER BY created_at DESC, id DESC
                """,
                (
                    self.domain,
                    manifest.import_id,
                    manifest.source_hash,
                    manifest.config_hash,
                    manifest.live_hash,
                    manifest.threshold_evaluation_hash,
                ),
            ).fetchall()
        latest: dict[str, sqlite3.Row] = {}
        for row in rows:
            kind = str(row["plan_kind"] or "ordinary")
            latest.setdefault(kind, row)
        ordinary = latest.get("ordinary")
        if ordinary is None or str(ordinary["status"]) != "completed":
            return False
        if any(
            kind in latest and str(latest[kind]["status"]) != "completed"
            for kind in ("archive", "ownership")
        ):
            return False
        self.mark_accepted(manifest.import_id, when=when)
        return True

    def get_scope_readiness(
        self,
        required_scope_hash: str,
        *,
        now: Optional[float] = None,
        ttl_seconds: int,
    ) -> ScopeReadiness:
        digest = _validate_digest(required_scope_hash)
        current = float(time.time() if now is None else now)
        with closing(self._conn()) as conn:
            row = conn.execute(
                """
                SELECT scope_hash, verified_at FROM scope_readiness
                WHERE domain = ?
                """,
                (self.domain,),
            ).fetchone()
        verified_at = float(row["verified_at"]) if row is not None else 0.0
        stored_hash = str(row["scope_hash"]) if row is not None else ""
        expires_at = verified_at + max(1, int(ttl_seconds))
        return ScopeReadiness(
            ready=bool(
                stored_hash == digest
                and verified_at > 0
                and current >= verified_at
                and current <= expires_at
            ),
            required_scope_hash=digest,
            verified_at=verified_at,
            expires_at=expires_at if verified_at else 0.0,
        )

    def mark_scope_ready(
        self,
        required_scope_hash: str,
        *,
        when: Optional[float] = None,
        ttl_seconds: int,
    ) -> ScopeReadiness:
        digest = _validate_digest(required_scope_hash)
        verified_at = float(time.time() if when is None else when)
        with closing(self._conn()) as conn, conn:
            conn.execute(
                """
                INSERT INTO scope_readiness(domain, scope_hash, verified_at)
                VALUES (?, ?, ?)
                ON CONFLICT(domain) DO UPDATE SET
                    scope_hash = excluded.scope_hash,
                    verified_at = excluded.verified_at
                """,
                (self.domain, digest, verified_at),
            )
        self._restrict_state_perms()
        return self.get_scope_readiness(
            digest,
            now=verified_at,
            ttl_seconds=ttl_seconds,
        )

    def invalidate_scope_readiness(self) -> None:
        """Forget cached scope proof after credentials or connector identity change."""

        with closing(self._conn()) as conn, conn:
            conn.execute(
                "DELETE FROM scope_readiness WHERE domain = ?",
                (self.domain,),
            )
        self._restrict_state_perms()

    def record_override(
        self,
        import_id: str,
        evaluation: ThresholdEvaluation,
        reason: str,
        typed_import_id: str,
    ) -> ThresholdOverride:
        self.get_import(import_id)
        if not evaluation.held:
            raise OneRosterError(
                "OR-OVERRIDE-NOT-HELD",
                "Only a held threshold evaluation can be overridden.",
            )
        if typed_import_id.strip() != import_id:
            raise OneRosterError(
                "OR-OVERRIDE-CONFIRMATION",
                "The typed import ID does not match this held import.",
            )
        safe_reason = reason.strip()
        if len(safe_reason) < 8:
            raise OneRosterError(
                "OR-OVERRIDE-REASON",
                "A meaningful audit reason is required to override a threshold hold.",
            )
        digest = evaluation_hash(evaluation)
        if self.get_denial(import_id, evaluation) is not None:
            raise OneRosterError(
                "OR-THRESHOLD-DENIED",
                "This exact held plan was denied. Replan after source, policy, or live state changes.",
            )
        recorded_at = time.time()
        with closing(self._conn()) as conn, conn:
            conn.execute(
                """
                INSERT INTO threshold_overrides(
                    domain, import_id, evaluation_hash, reason, recorded_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (self.domain, import_id, digest, safe_reason, recorded_at),
            )
        return ThresholdOverride(import_id, digest, safe_reason, recorded_at)

    def record_denial(
        self,
        import_id: str,
        evaluation: ThresholdEvaluation,
        reason: str,
        typed_import_id: str,
    ) -> ThresholdDenial:
        self.get_import(import_id)
        if not evaluation.held:
            raise OneRosterError(
                "OR-DENY-NOT-HELD",
                "Only a held threshold evaluation can be denied.",
            )
        if typed_import_id.strip() != import_id:
            raise OneRosterError(
                "OR-DENY-CONFIRMATION",
                "The typed import ID does not match this held import.",
            )
        safe_reason = reason.strip()
        if len(safe_reason) < 8 or len(safe_reason) > 500:
            raise OneRosterError(
                "OR-DENY-REASON",
                "A denial reason between 8 and 500 characters is required.",
            )
        digest = evaluation_hash(evaluation)
        if self.has_override(import_id, evaluation):
            raise OneRosterError(
                "OR-DENY-OVERRIDDEN",
                "This exact held plan already has an override and cannot also be denied.",
            )
        recorded_at = time.time()
        with closing(self._conn()) as conn, conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO threshold_denials(
                    domain, import_id, evaluation_hash, reason, recorded_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (self.domain, import_id, digest, safe_reason, recorded_at),
            )
            row = conn.execute(
                """
                SELECT reason, recorded_at FROM threshold_denials
                WHERE domain = ? AND import_id = ? AND evaluation_hash = ?
                """,
                (self.domain, import_id, digest),
            ).fetchone()
        assert row is not None
        return ThresholdDenial(
            import_id,
            digest,
            str(row["reason"]),
            float(row["recorded_at"]),
        )

    def get_denial(
        self,
        import_id: str,
        evaluation: ThresholdEvaluation,
    ) -> Optional[ThresholdDenial]:
        digest = evaluation_hash(evaluation)
        with closing(self._conn()) as conn:
            row = conn.execute(
                """
                SELECT reason, recorded_at FROM threshold_denials
                WHERE domain = ? AND import_id = ? AND evaluation_hash = ?
                """,
                (self.domain, import_id, digest),
            ).fetchone()
        if row is None:
            return None
        return ThresholdDenial(
            import_id,
            digest,
            str(row["reason"]),
            float(row["recorded_at"]),
        )

    def has_override(self, import_id: str, evaluation: ThresholdEvaluation) -> bool:
        with closing(self._conn()) as conn:
            row = conn.execute(
                """
                SELECT 1 FROM threshold_overrides
                WHERE domain = ? AND import_id = ? AND evaluation_hash = ?
                """,
                (self.domain, import_id, evaluation_hash(evaluation)),
            ).fetchone()
        return row is not None

    def create_manifest(
        self,
        import_id: str,
        *,
        config_hash: str,
        live_hash: str,
        actions: Sequence[ImportAction],
        threshold_evaluation_hash: str = "",
        limited_import: bool = False,
        plan_kind: str = "ordinary",
        threshold_evidence: Optional[Mapping[str, Any]] = None,
        exclusions: Sequence[Any] = (),
        pilot_evidence: Optional[Mapping[str, Any]] = None,
        live_evidence: Optional[Mapping[str, Any]] = None,
    ) -> ClassroomImportManifest:
        snapshot = self.get_import(import_id)
        if not snapshot.ready_for_apply:
            raise OneRosterError(
                "OR-IMPORT-BLOCKED",
                "Only a valid full snapshot can create an apply manifest.",
            )
        normalized_kind = str(plan_kind or "").strip().casefold()
        if normalized_kind not in {"ordinary", "limited", "archive", "ownership"}:
            raise ValueError("Invalid OneRoster plan kind.")
        selected_actions = _limited_actions(actions) if limited_import else actions
        threshold_record = _bounded_json_mapping(threshold_evidence or {})
        exclusion_records = tuple(_issue_record(item) for item in exclusions)
        pilot_record = _bounded_json_mapping(pilot_evidence or {})
        live_record = _bounded_live_evidence(live_evidence or {})
        manifest_id = secrets.token_hex(16)
        actions_hash, action_count = action_sequence_hash(selected_actions)
        basis = {
            "manifest_id": manifest_id,
            "domain": self.domain,
            "import_id": import_id,
            "source_hash": snapshot.source_sha256,
            "config_hash": config_hash,
            "live_hash": live_hash,
            "threshold_evaluation_hash": threshold_evaluation_hash,
            "threshold_evidence": threshold_record,
            "exclusions": exclusion_records,
            "pilot_evidence": pilot_record,
            "live_evidence": live_record,
            "plan_kind": normalized_kind,
            "actions_hash": actions_hash,
            "action_count": action_count,
        }
        manifest_hash = canonical_hash(basis)
        created_at = time.time()
        try:
            with closing(self._conn()) as conn, conn:
                conn.execute(
                    """
                    INSERT INTO manifests (
                        id, domain, import_id, source_hash, config_hash, live_hash,
                        manifest_hash, threshold_evaluation_hash, status, created_at, error,
                        plan_kind, confirmed_at, threshold_evidence_json,
                        exclusions_json, pilot_evidence_json, live_evidence_json,
                        drift_report_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'planned', ?, '', ?, 0, ?, ?, ?, ?, '[]')
                    """,
                    (
                        manifest_id,
                        self.domain,
                        import_id,
                        snapshot.source_sha256,
                        config_hash,
                        live_hash,
                        manifest_hash,
                        threshold_evaluation_hash,
                        created_at,
                        normalized_kind,
                        json.dumps(threshold_record, sort_keys=True, separators=(",", ":")),
                        json.dumps(exclusion_records, sort_keys=True, separators=(",", ":")),
                        json.dumps(pilot_record, sort_keys=True, separators=(",", ":")),
                        json.dumps(live_record, sort_keys=True, separators=(",", ":")),
                    ),
                )
                conn.executemany(
                    """
                    INSERT INTO manifest_actions (
                        manifest_id, action_id, kind, subject, target, before_value,
                        after_value, status, detail
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', '')
                    """,
                    (
                        (
                            manifest_id,
                            action.id,
                            action.kind,
                            action.subject,
                            action.target,
                            action.before,
                            action.after,
                        )
                        for action in selected_actions
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise ValueError("Manifest action IDs must be unique.") from exc
        header = self.get_manifest_header(manifest_id)
        return replace(header, actions=tuple(selected_actions))

    def record_manifest_drift(
        self,
        manifest_id: str,
        rows: Sequence[Mapping[str, Any]],
    ) -> ClassroomImportManifest:
        """Persist a bounded, privacy-safe field report for stale approval UI."""

        bounded = []
        for raw in rows[:100]:
            bounded.append(
                {
                    "category": str(raw.get("category", "live state changed"))[:64],
                    "course": str(raw.get("course", ""))[:512],
                    "field": str(raw.get("field", ""))[:128],
                    "approved": str(raw.get("approved", ""))[:2000],
                    "current": str(raw.get("current", ""))[:2000],
                }
            )
        with closing(self._conn()) as conn, conn:
            result = conn.execute(
                """
                UPDATE manifests SET drift_report_json = ?
                WHERE domain = ? AND id = ?
                """,
                (
                    json.dumps(bounded, sort_keys=True, separators=(",", ":")),
                    self.domain,
                    manifest_id,
                ),
            )
            if result.rowcount != 1:
                raise KeyError("OneRoster import manifest not found.")
        return self.get_manifest_header(manifest_id)

    def confirm_manifest(
        self,
        manifest_id: str,
        typed_import_id: str,
        *,
        when: Optional[float] = None,
    ) -> ClassroomImportManifest:
        manifest = self.get_manifest_header(manifest_id)
        if manifest.status not in {"planned", "awaiting_students"}:
            raise OneRosterError(
                "OR-MANIFEST-NOT-RUNNABLE",
                "This manifest is not awaiting an operator-confirmed execution.",
            )
        if typed_import_id.strip() != manifest.import_id:
            raise OneRosterError(
                "OR-CONFIRMATION-MISMATCH",
                "The typed import ID does not match this immutable manifest.",
            )
        with closing(self._conn()) as conn, conn:
            conn.execute(
                """
                UPDATE manifests SET confirmed_at = ?
                WHERE domain = ? AND id = ? AND confirmed_at = 0
                """,
                (
                    float(when if when is not None else time.time()),
                    self.domain,
                    manifest_id,
                ),
            )
        return self.get_manifest_header(manifest_id)

    def claim_manifest(
        self,
        manifest_id: str,
        *,
        allow_awaiting_students: bool = False,
        allow_paused: bool = False,
        allow_recovery: bool = False,
        owner_id: str = "",
        owner_pid: Optional[int] = None,
        owner_identity: Optional[str] = None,
    ) -> ClassroomImportManifest:
        pid = os.getpid() if owner_pid is None else int(owner_pid)
        owner = owner_id.strip() or f"pid:{pid}"
        identity = (
            current_process_identity()
            if owner_identity is None
            else owner_identity
        )
        allowed_values = ["planned"]
        if allow_awaiting_students:
            allowed_values.append("awaiting_students")
        if allow_paused:
            allowed_values.append("paused")
        if allow_recovery:
            allowed_values.append("recovery_required")
        allowed = tuple(allowed_values)
        placeholders = ",".join("?" for _ in allowed)
        with closing(self._conn()) as conn, conn:
            result = conn.execute(
                f"""
                UPDATE manifests
                SET status = 'running', error = '', run_owner = ?,
                    run_pid = ?, run_identity = ?
                WHERE domain = ? AND id = ? AND confirmed_at > 0
                  AND status IN ({placeholders})
                """,
                (owner, pid, identity, self.domain, manifest_id, *allowed),
            )
            if result.rowcount != 1:
                row = conn.execute(
                    "SELECT status, confirmed_at FROM manifests WHERE domain = ? AND id = ?",
                    (self.domain, manifest_id),
                ).fetchone()
                if row is None:
                    raise KeyError("OneRoster import manifest not found.")
                if not float(row["confirmed_at"] or 0):
                    raise OneRosterError(
                        "OR-CONFIRMATION-REQUIRED",
                        "Type the exact import ID before executing this manifest.",
                    )
                if str(row["status"]) == "interrupted":
                    raise OneRosterError(
                        "OR-MANIFEST-INTERRUPTED",
                        "Interrupted work must be replanned from live state and confirmed again.",
                    )
                raise OneRosterError(
                    "OR-MANIFEST-ACTIVE",
                    "This manifest is already running or is no longer runnable.",
                )
        return self.get_manifest_header(manifest_id)

    def owns_claim(self, manifest_id: str, owner_id: str) -> bool:
        """Return whether the exact executor token still owns the running lease."""

        with closing(self._conn()) as conn:
            row = conn.execute(
                """
                SELECT 1 FROM manifests
                WHERE domain = ? AND id = ? AND status = 'running'
                  AND run_owner = ?
                """,
                (self.domain, manifest_id, owner_id),
            ).fetchone()
        return row is not None

    def start_execution_run(
        self,
        manifest_id: str,
        *,
        phase: str,
        owner_id: str,
        now: Optional[float] = None,
    ) -> ExecutionRun:
        """Create a durable attempt after the caller has claimed the manifest."""

        _validate_id(manifest_id)
        owner = owner_id.strip()
        if not owner:
            raise ValueError("An execution owner is required.")
        timestamp = float(now if now is not None else time.time())
        run_id = secrets.token_hex(16)
        with closing(self._conn()) as conn, conn:
            manifest = conn.execute(
                """
                SELECT 1 FROM manifests
                WHERE domain = ? AND id = ? AND status = 'running'
                  AND run_owner = ?
                """,
                (self.domain, manifest_id, owner),
            ).fetchone()
            if manifest is None:
                raise PermissionError(
                    "The OneRoster manifest lease is owned by another executor."
                )
            active = conn.execute(
                """
                SELECT * FROM execution_runs
                WHERE domain = ? AND manifest_id = ?
                  AND status IN ('running', 'pause_requested', 'recovery_required')
                ORDER BY started_at DESC LIMIT 1
                """,
                (self.domain, manifest_id),
            ).fetchone()
            if active is not None:
                if str(active["status"]) == "recovery_required":
                    raise OneRosterError(
                        "OR-RECOVERY-REQUIRED",
                        "The previous execution must be reconciled before it can resume.",
                    )
                return _execution_run_from_row(active)
            row = conn.execute(
                """
                SELECT COALESCE(MAX(attempt_number), 0) + 1 AS attempt
                FROM execution_runs WHERE domain = ? AND manifest_id = ?
                """,
                (self.domain, manifest_id),
            ).fetchone()
            attempt = int(row["attempt"] or 1)
            conn.execute(
                """
                INSERT INTO execution_runs (
                    id, manifest_id, domain, status, phase, started_at,
                    updated_at, last_heartbeat_at, current_batch_sequence,
                    stop_requested, attempt_number, last_error_code
                ) VALUES (?, ?, ?, 'running', ?, ?, ?, ?, 0, 0, ?, '')
                """,
                (
                    run_id,
                    manifest_id,
                    self.domain,
                    str(phase or "preflight"),
                    timestamp,
                    timestamp,
                    timestamp,
                    attempt,
                ),
            )
            created = conn.execute(
                "SELECT * FROM execution_runs WHERE id = ?",
                (run_id,),
            ).fetchone()
        assert created is not None
        return _execution_run_from_row(created)

    def record_execution_planning_receipt(
        self,
        run_id: str,
        *,
        total_seconds: float,
        directory_snapshot_seconds: float,
        classroom_snapshot_seconds: float,
        now: Optional[float] = None,
    ) -> ExecutionRun:
        """Persist sanitized planning timings without storing source or tenant data."""

        timestamp = float(now if now is not None else time.time())
        with closing(self._conn()) as conn, conn:
            result = conn.execute(
                """
                UPDATE execution_runs
                SET planning_seconds = ?, directory_snapshot_seconds = ?,
                    classroom_snapshot_seconds = ?, updated_at = ?,
                    last_heartbeat_at = ?
                WHERE id = ? AND domain = ?
                  AND status IN ('running', 'pause_requested')
                """,
                (
                    max(0.0, float(total_seconds)),
                    max(0.0, float(directory_snapshot_seconds)),
                    max(0.0, float(classroom_snapshot_seconds)),
                    timestamp,
                    timestamp,
                    run_id,
                    self.domain,
                ),
            )
            if result.rowcount != 1:
                raise KeyError("Active OneRoster execution run not found.")
            row = conn.execute(
                "SELECT * FROM execution_runs WHERE id = ? AND domain = ?",
                (run_id, self.domain),
            ).fetchone()
        assert row is not None
        return _execution_run_from_row(row)

    def heartbeat_execution_run(
        self,
        run_id: str,
        *,
        phase: Optional[str] = None,
        now: Optional[float] = None,
    ) -> ExecutionRun:
        timestamp = float(now if now is not None else time.time())
        with closing(self._conn()) as conn, conn:
            result = conn.execute(
                """
                UPDATE execution_runs
                SET phase = COALESCE(?, phase), updated_at = ?, last_heartbeat_at = ?
                WHERE id = ? AND domain = ?
                  AND status IN ('running', 'pause_requested')
                """,
                (phase, timestamp, timestamp, run_id, self.domain),
            )
            if result.rowcount != 1:
                raise OneRosterError(
                    "OR-EXECUTION-RUN-INACTIVE",
                    "The execution run is no longer active.",
                )
            row = conn.execute(
                "SELECT * FROM execution_runs WHERE id = ? AND domain = ?",
                (run_id, self.domain),
            ).fetchone()
        assert row is not None
        return _execution_run_from_row(row)

    def claim_execution_recovery(
        self,
        manifest_id: str,
        *,
        owner_id: str,
        now: Optional[float] = None,
    ) -> ExecutionRun:
        """Attach a new manifest lease to the prior run that needs reconciliation."""

        timestamp = float(now if now is not None else time.time())
        with closing(self._conn()) as conn, conn:
            manifest = conn.execute(
                """
                SELECT 1 FROM manifests WHERE domain = ? AND id = ?
                  AND status = 'running' AND run_owner = ?
                """,
                (self.domain, manifest_id, owner_id),
            ).fetchone()
            if manifest is None:
                raise PermissionError("The recovery worker does not own this manifest.")
            result = conn.execute(
                """
                UPDATE execution_runs
                SET status = 'running', phase = 'reconciliation',
                    stop_requested = 0, updated_at = ?, last_heartbeat_at = ?
                WHERE domain = ? AND manifest_id = ?
                  AND status = 'recovery_required'
                """,
                (timestamp, timestamp, self.domain, manifest_id),
            )
            if result.rowcount != 1:
                raise OneRosterError(
                    "OR-RECOVERY-NOT-AVAILABLE",
                    "No interrupted durable batch is awaiting reconciliation.",
                )
            row = conn.execute(
                """
                SELECT * FROM execution_runs
                WHERE domain = ? AND manifest_id = ? AND status = 'running'
                ORDER BY started_at DESC LIMIT 1
                """,
                (self.domain, manifest_id),
            ).fetchone()
        assert row is not None
        return _execution_run_from_row(row)

    def get_reconciling_batches(
        self,
        manifest_id: str,
    ) -> tuple[ExecutionBatch, ...]:
        with closing(self._conn()) as conn:
            rows = conn.execute(
                """
                SELECT b.* FROM execution_batches AS b
                JOIN execution_runs AS r ON r.id = b.run_id
                WHERE r.domain = ? AND b.manifest_id = ?
                  AND b.status = 'reconciling'
                ORDER BY b.sequence_number
                """,
                (self.domain, manifest_id),
            ).fetchall()
            result = []
            for row in rows:
                action_rows = conn.execute(
                    """
                    SELECT action_id FROM execution_batch_actions
                    WHERE batch_id = ? ORDER BY ordinal
                    """,
                    (row["id"],),
                ).fetchall()
                result.append(
                    _execution_batch_from_row(
                        row,
                        tuple(str(item["action_id"]) for item in action_rows),
                    )
                )
        return tuple(result)

    def get_manifest_actions_by_id(
        self,
        manifest_id: str,
        action_ids: Sequence[str],
    ) -> tuple[ImportAction, ...]:
        selected = tuple(dict.fromkeys(str(item) for item in action_ids if str(item)))
        if not selected:
            return ()
        placeholders = ",".join("?" for _ in selected)
        with closing(self._conn()) as conn:
            rows = conn.execute(
                f"""
                SELECT * FROM manifest_actions
                WHERE manifest_id = ? AND action_id IN ({placeholders})
                """,
                (manifest_id, *selected),
            ).fetchall()
        by_id = {str(row["action_id"]): _action_from_row(row) for row in rows}
        if set(by_id) != set(selected):
            raise OneRosterError(
                "OR-RECOVERY-EVIDENCE-MISSING",
                "Durable batch membership no longer matches manifest actions.",
            )
        return tuple(by_id[action_id] for action_id in selected)

    def prepare_execution_batch(
        self,
        run_id: str,
        manifest_id: str,
        actions: Sequence[ImportAction],
        *,
        phase: str,
        owner_id: str,
        now: Optional[float] = None,
    ) -> ExecutionBatch:
        """Persist exact batch membership before any GAM command is attempted."""

        if not actions or len(actions) > 50:
            raise ValueError("An execution batch must contain between 1 and 50 actions.")
        action_ids = tuple(action.id for action in actions)
        if len(action_ids) != len(set(action_ids)):
            raise ValueError("Execution batch action IDs must be unique.")
        timestamp = float(now if now is not None else time.time())
        batch_id = secrets.token_hex(16)
        digest = canonical_hash(action_ids)
        with closing(self._conn()) as conn, conn:
            run = conn.execute(
                """
                SELECT r.*, m.run_owner, m.status AS manifest_status
                FROM execution_runs AS r
                JOIN manifests AS m ON m.id = r.manifest_id
                WHERE r.id = ? AND r.domain = ? AND r.manifest_id = ?
                """,
                (run_id, self.domain, manifest_id),
            ).fetchone()
            if (
                run is None
                or str(run["status"]) not in {"running", "pause_requested"}
                or str(run["manifest_status"]) != "running"
                or str(run["run_owner"]) != owner_id
            ):
                raise PermissionError("The execution run does not own this manifest.")
            placeholders = ",".join("?" for _ in action_ids)
            rows = conn.execute(
                f"""
                SELECT action_id FROM manifest_actions
                WHERE manifest_id = ? AND status = 'pending'
                  AND action_id IN ({placeholders})
                """,
                (manifest_id, *action_ids),
            ).fetchall()
            if {str(row["action_id"]) for row in rows} != set(action_ids):
                raise OneRosterError(
                    "OR-BATCH-ACTIONS-CHANGED",
                    "The selected batch no longer matches pending manifest actions.",
                )
            sequence_row = conn.execute(
                """
                SELECT COALESCE(MAX(sequence_number), 0) + 1 AS sequence
                FROM execution_batches WHERE run_id = ?
                """,
                (run_id,),
            ).fetchone()
            sequence = int(sequence_row["sequence"] or 1)
            conn.execute(
                """
                INSERT INTO execution_batches (
                    id, run_id, manifest_id, sequence_number, phase,
                    action_ids_hash, action_count, status, prepared_at,
                    started_at, completed_at, attempt_number, error_code
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'prepared', ?, 0, 0, 1, '')
                """,
                (
                    batch_id,
                    run_id,
                    manifest_id,
                    sequence,
                    str(phase or "apply"),
                    digest,
                    len(action_ids),
                    timestamp,
                ),
            )
            conn.executemany(
                """
                INSERT INTO execution_batch_actions (batch_id, action_id, ordinal)
                VALUES (?, ?, ?)
                """,
                (
                    (batch_id, action_id, ordinal)
                    for ordinal, action_id in enumerate(action_ids)
                ),
            )
            conn.execute(
                """
                UPDATE execution_runs
                SET phase = ?, current_batch_sequence = ?, updated_at = ?,
                    last_heartbeat_at = ?
                WHERE id = ? AND domain = ?
                """,
                (phase, sequence, timestamp, timestamp, run_id, self.domain),
            )
            row = conn.execute(
                "SELECT * FROM execution_batches WHERE id = ?",
                (batch_id,),
            ).fetchone()
        assert row is not None
        return _execution_batch_from_row(row, action_ids)

    def mark_execution_batch_started(
        self,
        batch_id: str,
        *,
        worker_count: int = 5,
        now: Optional[float] = None,
    ) -> ExecutionBatch:
        timestamp = float(now if now is not None else time.time())
        with closing(self._conn()) as conn, conn:
            result = conn.execute(
                """
                UPDATE execution_batches
                SET status = 'running', started_at = ?, worker_count = ?
                WHERE id = ? AND status = 'prepared'
                  AND run_id IN (SELECT id FROM execution_runs WHERE domain = ?)
                """,
                (timestamp, int(worker_count), batch_id, self.domain),
            )
            if result.rowcount != 1:
                raise OneRosterError(
                    "OR-BATCH-NOT-PREPARED",
                    "The durable execution batch is not ready to start.",
                )
            conn.execute(
                """
                UPDATE execution_runs SET updated_at = ?, last_heartbeat_at = ?
                WHERE id = (SELECT run_id FROM execution_batches WHERE id = ?)
                """,
                (timestamp, timestamp, batch_id),
            )
            row = conn.execute(
                "SELECT * FROM execution_batches WHERE id = ?",
                (batch_id,),
            ).fetchone()
            action_rows = conn.execute(
                """
                SELECT action_id FROM execution_batch_actions
                WHERE batch_id = ? ORDER BY ordinal
                """,
                (batch_id,),
            ).fetchall()
        assert row is not None
        return _execution_batch_from_row(
            row, tuple(str(item["action_id"]) for item in action_rows)
        )

    def finish_execution_batch(
        self,
        batch_id: str,
        *,
        status: str,
        error_code: str = "",
        now: Optional[float] = None,
    ) -> ExecutionBatch:
        if status not in {"completed", "failed", "reconciled"}:
            raise ValueError("Invalid execution batch terminal status.")
        timestamp = float(now if now is not None else time.time())
        with closing(self._conn()) as conn, conn:
            result = conn.execute(
                """
                UPDATE execution_batches
                SET status = ?, completed_at = ?, error_code = ?
                WHERE id = ? AND status IN ('running', 'reconciling')
                  AND run_id IN (SELECT id FROM execution_runs WHERE domain = ?)
                """,
                (status, timestamp, error_code, batch_id, self.domain),
            )
            if result.rowcount != 1:
                raise OneRosterError(
                    "OR-BATCH-NOT-ACTIVE",
                    "The durable execution batch is no longer active.",
                )
            conn.execute(
                """
                UPDATE execution_runs SET updated_at = ?, last_heartbeat_at = ?
                WHERE id = (SELECT run_id FROM execution_batches WHERE id = ?)
                """,
                (timestamp, timestamp, batch_id),
            )
            row = conn.execute(
                "SELECT * FROM execution_batches WHERE id = ?",
                (batch_id,),
            ).fetchone()
            action_rows = conn.execute(
                """
                SELECT action_id FROM execution_batch_actions
                WHERE batch_id = ? ORDER BY ordinal
                """,
                (batch_id,),
            ).fetchall()
        assert row is not None
        return _execution_batch_from_row(
            row, tuple(str(item["action_id"]) for item in action_rows)
        )

    def complete_verified_batch(
        self,
        batch_id: str,
        manifest_id: str,
        results: Mapping[str, tuple[str, str]],
        *,
        owner_id: str,
        apply_seconds: float,
        verification_seconds: float,
        verification_attempts: int,
        worker_count: int,
        throttling_count: int = 0,
        now: Optional[float] = None,
    ) -> ExecutionBatch:
        """Atomically save exact verified results and complete their durable batch."""

        owner = owner_id.strip()
        if not owner:
            raise ValueError("An execution owner is required.")
        normalized = {
            str(action_id): (str(status), str(detail))
            for action_id, (status, detail) in results.items()
        }
        if not normalized or any(
            status not in {"applied", "failed", "skipped"}
            for status, _detail in normalized.values()
        ):
            raise ValueError("Verified batch results are invalid.")
        timestamp = float(now if now is not None else time.time())
        persist_started = time.perf_counter()
        with closing(self._conn()) as conn, conn:
            batch = conn.execute(
                """
                SELECT b.*, r.status AS run_status, m.status AS manifest_status,
                       m.run_owner
                FROM execution_batches AS b
                JOIN execution_runs AS r ON r.id = b.run_id
                JOIN manifests AS m ON m.id = b.manifest_id
                WHERE b.id = ? AND b.manifest_id = ? AND r.domain = ?
                """,
                (batch_id, manifest_id, self.domain),
            ).fetchone()
            if (
                batch is None
                or str(batch["status"]) != "running"
                or str(batch["run_status"]) not in {"running", "pause_requested"}
                or str(batch["manifest_status"]) != "running"
                or str(batch["run_owner"]) != owner
            ):
                raise PermissionError(
                    "The active execution batch does not own this manifest."
                )
            membership_rows = conn.execute(
                """
                SELECT action_id FROM execution_batch_actions
                WHERE batch_id = ? ORDER BY ordinal
                """,
                (batch_id,),
            ).fetchall()
            action_ids = tuple(str(row["action_id"]) for row in membership_rows)
            if (
                set(action_ids) != set(normalized)
                or len(action_ids) != int(batch["action_count"])
                or canonical_hash(action_ids) != str(batch["action_ids_hash"])
            ):
                raise OneRosterError(
                    "OR-BATCH-ACTIONS-CHANGED",
                    "Verified results no longer match immutable batch membership.",
                )
            placeholders = ",".join("?" for _ in action_ids)
            pending_rows = conn.execute(
                f"""
                SELECT action_id FROM manifest_actions
                WHERE manifest_id = ? AND status = 'pending'
                  AND action_id IN ({placeholders})
                """,
                (manifest_id, *action_ids),
            ).fetchall()
            if {str(row["action_id"]) for row in pending_rows} != set(action_ids):
                raise OneRosterError(
                    "OR-BATCH-ACTIONS-CHANGED",
                    "Verified results no longer match pending manifest actions.",
                )
            updated = conn.executemany(
                """
                UPDATE manifest_actions SET status = ?, detail = ?
                WHERE manifest_id = ? AND action_id = ? AND status = 'pending'
                """,
                (
                    (normalized[action_id][0], normalized[action_id][1], manifest_id, action_id)
                    for action_id in action_ids
                ),
            )
            if updated.rowcount != len(action_ids):
                raise OneRosterError(
                    "OR-BATCH-ACTIONS-CHANGED",
                    "Not every immutable batch action could be updated.",
                )
            failed = any(status == "failed" for status, _detail in normalized.values())
            persistence_seconds = time.perf_counter() - persist_started
            conn.execute(
                """
                UPDATE execution_batches
                SET status = ?, completed_at = ?, error_code = ?,
                    apply_seconds = ?, verification_seconds = ?,
                    persistence_seconds = ?, verification_attempts = ?,
                    worker_count = ?, throttling_count = ?
                WHERE id = ? AND status = 'running'
                """,
                (
                    "failed" if failed else "completed",
                    timestamp,
                    "OR-BATCH-VERIFY-FAILED" if failed else "",
                    max(0.0, float(apply_seconds)),
                    max(0.0, float(verification_seconds)),
                    max(0.0, float(persistence_seconds)),
                    max(1, int(verification_attempts)),
                    int(worker_count),
                    max(0, int(throttling_count)),
                    batch_id,
                ),
            )
            conn.execute(
                """
                UPDATE execution_runs SET updated_at = ?, last_heartbeat_at = ?
                WHERE id = ? AND domain = ?
                """,
                (timestamp, timestamp, str(batch["run_id"]), self.domain),
            )
            row = conn.execute(
                "SELECT * FROM execution_batches WHERE id = ?",
                (batch_id,),
            ).fetchone()
        assert row is not None
        return _execution_batch_from_row(row, action_ids)

    def request_execution_pause(
        self,
        manifest_id: str,
        *,
        now: Optional[float] = None,
    ) -> ExecutionRun:
        """Request a safe stop; the executor observes it only between batches."""

        timestamp = float(now if now is not None else time.time())
        with closing(self._conn()) as conn, conn:
            result = conn.execute(
                """
                UPDATE execution_runs
                SET stop_requested = 1, status = 'pause_requested', updated_at = ?
                WHERE domain = ? AND manifest_id = ? AND status = 'running'
                """,
                (timestamp, self.domain, manifest_id),
            )
            if result.rowcount != 1:
                raise OneRosterError(
                    "OR-EXECUTION-NOT-RUNNING",
                    "There is no running execution to pause.",
                )
            row = conn.execute(
                """
                SELECT * FROM execution_runs
                WHERE domain = ? AND manifest_id = ? AND status = 'pause_requested'
                """,
                (self.domain, manifest_id),
            ).fetchone()
        assert row is not None
        return _execution_run_from_row(row)

    def execution_stop_requested(self, run_id: str) -> bool:
        with closing(self._conn()) as conn:
            row = conn.execute(
                """
                SELECT stop_requested FROM execution_runs
                WHERE id = ? AND domain = ?
                """,
                (run_id, self.domain),
            ).fetchone()
        if row is None:
            raise KeyError("OneRoster execution run not found.")
        return bool(row["stop_requested"])

    def finish_execution_run(
        self,
        run_id: str,
        *,
        status: str,
        error_code: str = "",
        phase: Optional[str] = None,
        now: Optional[float] = None,
    ) -> ExecutionRun:
        if status not in {
            "completed", "failed", "paused", "recovery_required", "stale"
        }:
            raise ValueError("Invalid execution run terminal status.")
        timestamp = float(now if now is not None else time.time())
        with closing(self._conn()) as conn, conn:
            result = conn.execute(
                """
                UPDATE execution_runs
                SET status = ?, phase = COALESCE(?, phase), updated_at = ?,
                    last_heartbeat_at = ?, last_error_code = ?
                WHERE id = ? AND domain = ?
                  AND status IN ('running', 'pause_requested', 'recovery_required')
                """,
                (status, phase, timestamp, timestamp, error_code, run_id, self.domain),
            )
            if result.rowcount != 1:
                raise OneRosterError(
                    "OR-EXECUTION-RUN-INACTIVE",
                    "The execution run is no longer active.",
                )
            row = conn.execute(
                "SELECT * FROM execution_runs WHERE id = ? AND domain = ?",
                (run_id, self.domain),
            ).fetchone()
        assert row is not None
        return _execution_run_from_row(row)

    def get_execution_progress(
        self,
        manifest_id: str,
        *,
        now: Optional[float] = None,
        batch_size: int = 50,
    ) -> ExecutionProgress:
        timestamp = float(now if now is not None else time.time())
        with closing(self._conn()) as conn:
            manifest = conn.execute(
                "SELECT 1 FROM manifests WHERE domain = ? AND id = ?",
                (self.domain, manifest_id),
            ).fetchone()
            if manifest is None:
                raise KeyError("OneRoster import manifest not found.")
            action_rows = conn.execute(
                """
                SELECT kind, COUNT(*) AS total,
                    COALESCE(SUM(CASE WHEN status = 'pending' THEN 1 ELSE 0 END), 0)
                        AS pending,
                    COALESCE(SUM(CASE WHEN status = 'applied' THEN 1 ELSE 0 END), 0)
                        AS applied,
                    COALESCE(SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END), 0)
                        AS failed,
                    COALESCE(SUM(CASE WHEN status = 'skipped' THEN 1 ELSE 0 END), 0)
                        AS skipped
                FROM manifest_actions WHERE manifest_id = ? GROUP BY kind
                """,
                (manifest_id,),
            ).fetchall()
            course_counts = conn.execute(
                """
                SELECT COUNT(DISTINCT subject) AS total,
                    COUNT(DISTINCT CASE WHEN status = 'pending' THEN subject END)
                        AS pending
                FROM manifest_actions WHERE manifest_id = ?
                """,
                (manifest_id,),
            ).fetchone()
            run_row = conn.execute(
                """
                SELECT * FROM execution_runs
                WHERE domain = ? AND manifest_id = ?
                ORDER BY started_at DESC LIMIT 1
                """,
                (self.domain, manifest_id),
            ).fetchone()
            completed_batches = 0
            batch_actions = 0
            batch_seconds = 0.0
            worker_count = 5
            adaptive_state = "normal"
            latest_batch = None
            if run_row is not None:
                batch_row = conn.execute(
                    """
                    SELECT COALESCE(SUM(status IN ('completed','reconciled')), 0) AS count,
                           COALESCE(SUM(
                               CASE WHEN status IN ('completed','failed','reconciled')
                               THEN action_count ELSE 0 END
                           ), 0) AS actions,
                           COALESCE(SUM(
                               CASE WHEN status IN ('completed','failed','reconciled')
                               THEN apply_seconds + verification_seconds + persistence_seconds
                               ELSE 0 END
                           ), 0) AS seconds
                    FROM execution_batches WHERE run_id = ?
                    """,
                    (run_row["id"],),
                ).fetchone()
                completed_batches = int(batch_row["count"] or 0)
                batch_actions = int(batch_row["actions"] or 0)
                batch_seconds = float(batch_row["seconds"] or 0)
                latest_batch = conn.execute(
                    """
                    SELECT b.*, COUNT(DISTINCT actions.subject) AS course_count
                    FROM execution_batches AS b
                    LEFT JOIN execution_batch_actions AS membership
                        ON membership.batch_id = b.id
                    LEFT JOIN manifest_actions AS actions
                        ON actions.manifest_id = b.manifest_id
                        AND actions.action_id = membership.action_id
                    WHERE b.run_id = ?
                    GROUP BY b.id
                    ORDER BY b.sequence_number DESC LIMIT 1
                    """,
                    (run_row["id"],),
                ).fetchone()
                if latest_batch is not None:
                    worker_count = int(latest_batch["worker_count"] or 5)
                    adaptive_state = (
                        "protecting_google"
                        if worker_count <= 3
                        or int(latest_batch["throttling_count"] or 0) > 0
                        or int(latest_batch["verification_attempts"] or 0) > 1
                        or str(latest_batch["status"]) == "failed"
                        else "normal"
                    )
        phase_totals: dict[str, dict[str, int | str]] = {
            key: {
                "key": key,
                "label": label,
                "total": 0,
                "pending": 0,
                "applied": 0,
                "failed": 0,
                "skipped": 0,
            }
            for key, label, _kinds in _PROGRESS_PHASES
        }
        for row in action_rows:
            phase_key, phase_label = _progress_phase(str(row["kind"] or ""))
            bucket = phase_totals.setdefault(
                phase_key,
                {
                    "key": phase_key,
                    "label": phase_label,
                    "total": 0,
                    "pending": 0,
                    "applied": 0,
                    "failed": 0,
                    "skipped": 0,
                },
            )
            for field_name in ("total", "pending", "applied", "failed", "skipped"):
                bucket[field_name] = int(bucket[field_name]) + int(row[field_name] or 0)
        phases = tuple(
            ExecutionPhaseProgress(
                key=str(bucket["key"]),
                label=str(bucket["label"]),
                total=int(bucket["total"]),
                finished=(int(bucket["total"]) - int(bucket["pending"])),
                applied=int(bucket["applied"]),
                failed=int(bucket["failed"]),
                skipped=int(bucket["skipped"]),
                pending=int(bucket["pending"]),
            )
            for bucket in phase_totals.values()
            if int(bucket["total"]) > 0
        )
        total = sum(phase.total for phase in phases)
        pending = sum(phase.pending for phase in phases)
        applied = sum(phase.applied for phase in phases)
        failed = sum(phase.failed for phase in phases)
        skipped = sum(phase.skipped for phase in phases)
        complete = applied + failed + skipped
        run = _execution_run_from_row(run_row) if run_row is not None else None
        elapsed = max(0.0, timestamp - run.started_at) if run else 0.0
        rate = (
            batch_actions * 60.0 / batch_seconds
            if batch_actions and batch_seconds > 0
            else (complete * 60.0 / elapsed if complete and elapsed > 0 else 0.0)
        )
        eta = (
            pending * 60.0 / rate
            if completed_batches >= 2 and pending and rate > 0
            else None
        )
        effective_batch_size = max(1, min(int(batch_size or 50), 50))
        estimated_batches = (total + effective_batch_size - 1) // effective_batch_size
        heartbeat_age = max(0.0, timestamp - run.last_heartbeat_at) if run else 0.0
        heartbeat_active = bool(
            run and run.status in {"running", "pause_requested"}
        )
        heartbeat_stale = bool(heartbeat_active and heartbeat_age >= 180.0)
        heartbeat_delayed = bool(heartbeat_active and heartbeat_age >= 30.0)
        batch_progress = None
        if latest_batch is not None:
            phase_key, phase_label = _progress_phase(str(latest_batch["phase"] or ""))
            batch_progress = ExecutionBatchProgress(
                sequence_number=int(latest_batch["sequence_number"] or 0),
                phase=str(latest_batch["phase"] or ""),
                phase_key=phase_key,
                phase_label=phase_label,
                action_count=int(latest_batch["action_count"] or 0),
                course_count=int(latest_batch["course_count"] or 0),
                status=str(latest_batch["status"] or ""),
                apply_seconds=float(latest_batch["apply_seconds"] or 0),
                verification_seconds=float(
                    latest_batch["verification_seconds"] or 0
                ),
                persistence_seconds=float(latest_batch["persistence_seconds"] or 0),
                verification_attempts=int(
                    latest_batch["verification_attempts"] or 0
                ),
                worker_count=int(latest_batch["worker_count"] or 5),
                throttling_count=int(latest_batch["throttling_count"] or 0),
            )
        return ExecutionProgress(
            run=run,
            total=total,
            pending=pending,
            applied=applied,
            failed=failed,
            skipped=skipped,
            percent=(complete * 100.0 / total) if total else 100.0,
            completed_batches=completed_batches,
            total_batches_estimate=estimated_batches,
            elapsed_seconds=elapsed,
            heartbeat_age_seconds=heartbeat_age,
            heartbeat_stale=heartbeat_stale,
            actions_per_minute=rate,
            eta_seconds=eta,
            worker_count=worker_count,
            adaptive_state=adaptive_state,
            heartbeat_delayed=heartbeat_delayed,
            heartbeat_state=(
                "stale" if heartbeat_stale else "delayed" if heartbeat_delayed else "current"
            ),
            course_count=int(course_counts["total"] or 0) if course_counts else 0,
            remaining_course_count=(
                int(course_counts["pending"] or 0) if course_counts else 0
            ),
            phases=phases,
            current_batch=batch_progress,
        )

    def mark_running_interrupted(self) -> int:
        """Backward-compatible alias for definite-death lease recovery."""

        return self.recover_interrupted()

    def recover_interrupted(self) -> int:
        """Move definitely-dead leases to reconciliation without guessing outcomes."""

        recovered = 0
        with closing(self._conn()) as conn, conn:
            running = conn.execute(
                """
                SELECT id, run_owner, run_pid, run_identity
                FROM manifests
                WHERE domain = ? AND status = 'running'
                """,
                (self.domain,),
            ).fetchall()
            for manifest in running:
                pid = int(manifest["run_pid"] or 0)
                identity = str(manifest["run_identity"] or "")
                if not process_lease_is_dead(pid, identity):
                    continue
                run = conn.execute(
                    """
                    SELECT id FROM execution_runs
                    WHERE domain = ? AND manifest_id = ?
                      AND status IN ('running', 'pause_requested')
                    ORDER BY started_at DESC LIMIT 1
                    """,
                    (self.domain, manifest["id"]),
                ).fetchone()
                if run is not None:
                    timestamp = time.time()
                    conn.execute(
                        """
                        UPDATE execution_runs
                        SET status = 'recovery_required', phase = 'reconciliation',
                            updated_at = ?, last_error_code = 'OR-RECOVERY-REQUIRED'
                        WHERE id = ?
                        """,
                        (timestamp, run["id"]),
                    )
                    conn.execute(
                        """
                        UPDATE execution_batches SET status = 'reconciling',
                            error_code = 'OR-EXECUTION-INTERRUPTED'
                        WHERE run_id = ? AND status = 'running'
                        """,
                        (run["id"],),
                    )
                    conn.execute(
                        """
                        UPDATE execution_batches SET status = 'abandoned',
                            error_code = 'OR-BATCH-NOT-STARTED'
                        WHERE run_id = ? AND status = 'prepared'
                        """,
                        (run["id"],),
                    )
                    recovered_status = "recovery_required"
                    recovered_error = "OR-RECOVERY-REQUIRED"
                else:
                    # Legacy executions have no durable batch boundary to reconcile.
                    recovered_status = "interrupted"
                    recovered_error = "OR-EXECUTION-INTERRUPTED"
                result = conn.execute(
                    """
                    UPDATE manifests
                    SET status = ?, error = ?,
                        run_owner = '', run_pid = 0, run_identity = ''
                    WHERE domain = ? AND id = ? AND status = 'running'
                      AND run_owner = ? AND run_pid = ? AND run_identity = ?
                    """,
                    (
                        recovered_status,
                        recovered_error,
                        self.domain,
                        manifest["id"],
                        str(manifest["run_owner"] or ""),
                        pid,
                        identity,
                    ),
                )
                recovered += int(result.rowcount)
        if recovered:
            self._restrict_state_perms()
        return recovered

    def record_prepared_live_hash(
        self,
        manifest_id: str,
        live_hash: str,
        *,
        owner_id: str = "",
    ) -> ClassroomImportManifest:
        """Bind the student-release stage to the verified post-prep live state."""

        _validate_id(manifest_id)
        digest = _validate_digest(live_hash)
        owner = owner_id.strip() or f"pid:{os.getpid()}"
        with closing(self._conn()) as conn, conn:
            result = conn.execute(
                """
                UPDATE manifests SET prepared_live_hash = ?
                WHERE domain = ? AND id = ? AND status = 'running'
                  AND prepared_live_hash = '' AND run_owner = ?
                """,
                (digest, self.domain, manifest_id, owner),
            )
            if result.rowcount != 1:
                raise OneRosterError(
                    "OR-MANIFEST-DRIFT",
                    "The post-preparation live-state evidence could not be recorded.",
                )
        return self.get_manifest_header(manifest_id)

    def has_active_jobs(self) -> bool:
        """Return whether this domain has a persisted manifest actively executing."""
        with closing(self._conn()) as conn:
            row = conn.execute(
                """
                SELECT 1 FROM manifests
                WHERE domain = ? AND status = 'running'
                LIMIT 1
                """,
                (self.domain,),
            ).fetchone()
        return row is not None

    def get_manifest(self, manifest_id: str) -> ClassroomImportManifest:
        _validate_id(manifest_id)
        with closing(self._conn()) as conn:
            row = conn.execute(
                "SELECT * FROM manifests WHERE domain = ? AND id = ?",
                (self.domain, manifest_id),
            ).fetchone()
            if row is None:
                raise KeyError("OneRoster import manifest not found.")
            actions = conn.execute(
                """
                SELECT * FROM manifest_actions WHERE manifest_id = ?
                ORDER BY rowid
                """,
                (manifest_id,),
            ).fetchall()
        return _manifest_from_rows(row, actions)

    def get_manifest_header(self, manifest_id: str) -> ClassroomImportManifest:
        """Load immutable manifest metadata without materializing its actions."""

        _validate_id(manifest_id)
        with closing(self._conn()) as conn:
            row = conn.execute(
                "SELECT * FROM manifests WHERE domain = ? AND id = ?",
                (self.domain, manifest_id),
            ).fetchone()
        if row is None:
            raise KeyError("OneRoster import manifest not found.")
        return _manifest_from_rows(row, ())

    def latest_manifest_header(
        self,
        import_id: Optional[str] = None,
    ) -> Optional[ClassroomImportManifest]:
        """Return the most actionable recent manifest without loading actions.

        A single comparison can seal ordinary, archive, and ownership manifests at
        the same instant. Prefer the item needing attention over a merely newer
        sibling so a reopened guided journey resumes at the truthful current step.
        """

        if import_id is not None:
            _validate_id(import_id)
        sql = "SELECT * FROM manifests WHERE domain = ?"
        params: list[object] = [self.domain]
        if import_id is not None:
            sql += " AND import_id = ?"
            params.append(import_id)
        sql += """
            ORDER BY
              CASE
                WHEN status = 'recovery_required' THEN 0
                WHEN status IN ('running', 'pause_requested', 'paused') THEN 1
                WHEN status IN ('failed', 'interrupted', 'stale', 'partial') THEN 2
                WHEN status IN ('planned', 'awaiting_students') THEN 3
                WHEN status = 'completed' THEN 4
                ELSE 5
              END,
              CASE plan_kind
                WHEN 'ordinary' THEN 0
                WHEN 'archive' THEN 1
                WHEN 'ownership' THEN 2
                ELSE 3
              END,
              created_at DESC,
              id DESC
            LIMIT 1
        """
        with closing(self._conn()) as conn:
            row = conn.execute(sql, tuple(params)).fetchone()
        return _manifest_from_rows(row, ()) if row is not None else None

    def pending_action_kinds(self, manifest_id: str) -> tuple[str, ...]:
        """Return distinct pending action kinds without loading action records."""

        _validate_id(manifest_id)
        with closing(self._conn()) as conn:
            if not conn.execute(
                "SELECT 1 FROM manifests WHERE domain = ? AND id = ?",
                (self.domain, manifest_id),
            ).fetchone():
                raise KeyError("OneRoster import manifest not found.")
            rows = conn.execute(
                """
                SELECT DISTINCT kind FROM manifest_actions
                WHERE manifest_id = ? AND status = 'pending'
                ORDER BY kind
                """,
                (manifest_id,),
            ).fetchall()
        return tuple(str(row["kind"]) for row in rows)

    def pending_action_summary(self, manifest_id: str) -> Mapping[str, int]:
        """Count pending work in SQL, including the student-gate split."""

        _validate_id(manifest_id)
        with closing(self._conn()) as conn:
            if not conn.execute(
                "SELECT 1 FROM manifests WHERE domain = ? AND id = ?",
                (self.domain, manifest_id),
            ).fetchone():
                raise KeyError("OneRoster import manifest not found.")
            row = conn.execute(
                """
                SELECT
                    COUNT(*) AS total,
                    COALESCE(SUM(CASE WHEN kind IN ('student_add','student_remove')
                                      THEN 1 ELSE 0 END), 0) AS student,
                    COALESCE(SUM(CASE WHEN kind NOT IN ('student_add','student_remove')
                                      THEN 1 ELSE 0 END), 0) AS nonstudent
                FROM manifest_actions
                WHERE manifest_id = ? AND status = 'pending'
                """,
                (manifest_id,),
            ).fetchone()
        assert row is not None
        return {
            "total": int(row["total"] or 0),
            "student": int(row["student"] or 0),
            "nonstudent": int(row["nonstudent"] or 0),
        }

    def get_pending_action_batch(
        self,
        manifest_id: str,
        *,
        kinds: Sequence[str],
        limit: int = 50,
    ) -> tuple[ImportAction, ...]:
        """Read one bounded pending batch for explicitly allowlisted action kinds."""

        _validate_id(manifest_id)
        selected_kinds = tuple(
            dict.fromkeys(str(kind or "").strip() for kind in kinds if str(kind or "").strip())
        )
        if not selected_kinds:
            return ()
        batch_size = max(1, min(int(limit or 50), 50))
        placeholders = ",".join("?" for _ in selected_kinds)
        with closing(self._conn()) as conn:
            rows = conn.execute(
                f"""
                SELECT a.* FROM manifest_actions AS a
                JOIN manifests AS m ON m.id = a.manifest_id
                WHERE m.domain = ? AND a.manifest_id = ?
                  AND a.status = 'pending' AND a.kind IN ({placeholders})
                ORDER BY a.subject COLLATE NOCASE, a.target COLLATE NOCASE, a.rowid
                LIMIT ?
                """,
                (self.domain, manifest_id, *selected_kinds, batch_size),
            ).fetchall()
            if not rows and not conn.execute(
                "SELECT 1 FROM manifests WHERE domain = ? AND id = ?",
                (self.domain, manifest_id),
            ).fetchone():
                raise KeyError("OneRoster import manifest not found.")
        return tuple(_action_from_row(row) for row in rows)

    def pending_actions_hash(self, manifest_id: str) -> tuple[str, int]:
        """Hash pending action basis rows incrementally in immutable row order."""

        _validate_id(manifest_id)
        with closing(self._conn()) as conn:
            if not conn.execute(
                "SELECT 1 FROM manifests WHERE domain = ? AND id = ?",
                (self.domain, manifest_id),
            ).fetchone():
                raise KeyError("OneRoster import manifest not found.")
            cursor = conn.execute(
                """
                SELECT * FROM manifest_actions
                WHERE manifest_id = ? AND status = 'pending'
                ORDER BY rowid
                """,
                (manifest_id,),
            )

            def rows() -> Iterable[ImportAction]:
                while batch := cursor.fetchmany(512):
                    for row in batch:
                        yield _action_from_row(row)

            return action_sequence_hash(rows())

    def get_manifest_page(
        self,
        manifest_id: str,
        *,
        offset: int = 0,
        limit: int = MAX_PAGE_SIZE,
    ) -> ManifestPage:
        """Load one bounded action page and aggregate progress in SQL."""

        _validate_id(manifest_id)
        page_size = max(1, min(int(limit or MAX_PAGE_SIZE), MAX_PAGE_SIZE))
        requested_offset = max(0, int(offset or 0))
        with closing(self._conn()) as conn:
            row = conn.execute(
                "SELECT * FROM manifests WHERE domain = ? AND id = ?",
                (self.domain, manifest_id),
            ).fetchone()
            if row is None:
                raise KeyError("OneRoster import manifest not found.")
            aggregate = conn.execute(
                """
                SELECT
                    COUNT(*) AS total,
                    COALESCE(SUM(CASE WHEN status IN ('applied','failed','skipped')
                                      THEN 1 ELSE 0 END), 0) AS complete_count,
                    COALESCE(SUM(CASE WHEN status = 'pending' THEN 1 ELSE 0 END), 0)
                        AS pending_count
                FROM manifest_actions WHERE manifest_id = ?
                """,
                (manifest_id,),
            ).fetchone()
            total = int(aggregate["total"] or 0)
            page_offset = requested_offset
            if total and page_offset >= total:
                page_offset = ((total - 1) // page_size) * page_size
            actions = conn.execute(
                """
                SELECT * FROM manifest_actions WHERE manifest_id = ?
                ORDER BY rowid LIMIT ? OFFSET ?
                """,
                (manifest_id, page_size, page_offset),
            ).fetchall()
            counts = conn.execute(
                """
                SELECT kind, COUNT(*) AS count
                FROM manifest_actions WHERE manifest_id = ?
                GROUP BY kind ORDER BY kind
                """,
                (manifest_id,),
            ).fetchall()
        return ManifestPage(
            manifest=_manifest_from_rows(row, actions),
            total=total,
            offset=page_offset,
            limit=page_size,
            complete_count=int(aggregate["complete_count"] or 0),
            pending_count=int(aggregate["pending_count"] or 0),
            action_counts={str(item["kind"]): int(item["count"]) for item in counts},
        )

    def mark_action_result(
        self,
        manifest_id: str,
        action_id: str,
        *,
        status: str,
        detail: str = "",
        load_manifest: bool = True,
        owner_id: str = "",
    ) -> Optional[ClassroomImportManifest]:
        if status not in {"applied", "failed", "skipped"}:
            raise ValueError("Manifest action result must be applied, failed, or skipped.")
        owner = owner_id.strip() or f"pid:{os.getpid()}"
        with closing(self._conn()) as conn, conn:
            result = conn.execute(
                """
                UPDATE manifest_actions SET status = ?, detail = ?
                WHERE manifest_id = ? AND action_id = ?
                  AND EXISTS (
                    SELECT 1 FROM manifests WHERE id = ? AND domain = ?
                      AND status = 'running' AND run_owner = ?
                  )
                """,
                (
                    status,
                    detail,
                    manifest_id,
                    action_id,
                    manifest_id,
                    self.domain,
                    owner,
                ),
            )
            if result.rowcount != 1:
                exists = conn.execute(
                    """
                    SELECT 1 FROM manifest_actions
                    WHERE manifest_id = ? AND action_id = ?
                    """,
                    (manifest_id, action_id),
                ).fetchone()
                if exists is None:
                    raise KeyError("Pending OneRoster manifest action not found.")
                raise PermissionError(
                    "The OneRoster manifest lease is owned by another executor."
                )
        return self.get_manifest(manifest_id) if load_manifest else None

    def finish_manifest(
        self,
        manifest_id: str,
        *,
        status: str,
        error: str = "",
        load_actions: bool = True,
        owner_id: str = "",
    ) -> ClassroomImportManifest:
        if status not in {
            "completed",
            "partial",
            "failed",
            "interrupted",
            "paused",
            "recovery_required",
            "stale",
            "awaiting_students",
        }:
            raise ValueError("Invalid OneRoster manifest terminal status.")
        owner = owner_id.strip() or f"pid:{os.getpid()}"
        with closing(self._conn()) as conn, conn:
            result = conn.execute(
                """
                UPDATE manifests
                SET status = ?, error = ?,
                    run_owner = '', run_pid = 0, run_identity = ''
                WHERE domain = ? AND id = ?
                  AND (status != 'running' OR run_owner = ?)
                """,
                (status, error, self.domain, manifest_id, owner),
            )
            if result.rowcount != 1:
                exists = conn.execute(
                    "SELECT 1 FROM manifests WHERE domain = ? AND id = ?",
                    (self.domain, manifest_id),
                ).fetchone()
                if exists is None:
                    raise KeyError("OneRoster import manifest not found.")
                raise PermissionError(
                    "The OneRoster manifest lease is owned by another executor."
                )
        return (
            self.get_manifest(manifest_id)
            if load_actions
            else self.get_manifest_header(manifest_id)
        )

    def has_override_hash(self, import_id: str, digest: str) -> bool:
        if not digest:
            return False
        with closing(self._conn()) as conn:
            row = conn.execute(
                """
                SELECT 1 FROM threshold_overrides
                WHERE domain = ? AND import_id = ? AND evaluation_hash = ?
                """,
                (self.domain, import_id, digest),
            ).fetchone()
        return row is not None

    def get_gate(self) -> StudentEnrollmentGate:
        with closing(self._conn()) as conn:
            row = conn.execute(
                "SELECT * FROM student_gate WHERE domain = ?",
                (self.domain,),
            ).fetchone()
        return _gate_from_row(row) if row else closed_gate()

    def save_gate(self, gate: StudentEnrollmentGate) -> StudentEnrollmentGate:
        with closing(self._conn()) as conn, conn:
            conn.execute(
                """
                INSERT INTO student_gate (
                    domain, state, timezone, manifest_id, manifest_hash, release_at,
                    updated_at, hold_code, hold_detail
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(domain) DO UPDATE SET
                    state = excluded.state, timezone = excluded.timezone,
                    manifest_id = excluded.manifest_id,
                    manifest_hash = excluded.manifest_hash,
                    release_at = excluded.release_at,
                    updated_at = excluded.updated_at,
                    hold_code = excluded.hold_code,
                    hold_detail = excluded.hold_detail
                """,
                (
                    self.domain,
                    gate.state.value,
                    gate.timezone,
                    gate.manifest_id,
                    gate.manifest_hash,
                    gate.release_at,
                    gate.updated_at,
                    gate.hold_code,
                    gate.hold_detail,
                ),
            )
        return gate

    def close_gate(self) -> StudentEnrollmentGate:
        return self.save_gate(closed_gate())

    def hold_gate(
        self,
        code: str,
        detail: str,
        *,
        now: Optional[datetime] = None,
        preserve_manifest: bool = True,
    ) -> StudentEnrollmentGate:
        """Persist a fail-closed gate while retaining its audit subject.

        A scheduled-release hold must stop execution, but operators still need
        the exact manifest and release time that failed revalidation.  Earlier
        holds discarded that evidence, which also made the visible hold harder
        to investigate.
        """

        current = self.get_gate()
        held = gate_hold(code, detail, now=now)
        if preserve_manifest and current.manifest_id:
            held = StudentEnrollmentGate(
                state=held.state,
                timezone=held.timezone,
                manifest_id=current.manifest_id,
                manifest_hash=current.manifest_hash,
                release_at=current.release_at,
                updated_at=held.updated_at,
                hold_code=held.hold_code,
                hold_detail=held.hold_detail,
            )
        return self.save_gate(held)

    def previous_accepted_import_id(self, import_id: str) -> str:
        snapshot = self.get_import(import_id)
        with closing(self._conn()) as conn:
            row = conn.execute(
                """
                SELECT id FROM imports
                WHERE domain = ? AND accepted_at > 0 AND imported_at < ?
                ORDER BY accepted_at DESC LIMIT 1
                """,
                (self.domain, snapshot.imported_at),
            ).fetchone()
        if row is None:
            return ""
        return str(row["id"])

    def previous_accepted_aliases(self, import_id: str) -> tuple[str, ...]:
        previous_id = self.previous_accepted_import_id(import_id)
        if not previous_id:
            return ()
        with closing(self._conn()) as conn:
            rows = conn.execute(
                """
                SELECT alias FROM accepted_managed_aliases
                WHERE domain = ? AND import_id = ?
                ORDER BY alias COLLATE NOCASE
                """,
                (self.domain, previous_id),
            ).fetchall()
        aliases = tuple(str(row["alias"]) for row in rows)
        if aliases or not self.normalized_path(previous_id).is_file():
            return aliases

        # One-time migration for a baseline accepted before the host table
        # existed. Material is still available, so preserve it before expiry.
        aliases = _managed_aliases_from_snapshot(
            self.normalized_path(previous_id),
            self.domain,
        )
        with closing(self._conn()) as conn, conn:
            accepted_at = float(
                conn.execute(
                    """
                    SELECT accepted_at FROM imports
                    WHERE domain = ? AND id = ?
                    """,
                    (self.domain, previous_id),
                ).fetchone()[0]
            )
            conn.executemany(
                """
                INSERT OR IGNORE INTO accepted_managed_aliases (
                    domain, import_id, alias, accepted_at
                ) VALUES (?, ?, ?, ?)
                """,
                [
                    (self.domain, previous_id, alias, accepted_at)
                    for alias in aliases
                ],
            )
        return aliases

    def arm_gate(
        self,
        manifest_id: str,
        manifest_hash: str,
        release_at: str,
    ) -> StudentEnrollmentGate:
        manifest = self.get_manifest_header(manifest_id)
        if manifest.manifest_hash != manifest_hash:
            raise OneRosterError(
                "OR-GATE-MANIFEST-DRIFT",
                "The manifest hash does not match the persisted immutable manifest.",
            )
        if not manifest.confirmed or manifest.status != "awaiting_students":
            raise OneRosterError(
                "OR-GATE-MANIFEST-NOT-READY",
                "Student release can arm only after confirmed teacher preparation is complete.",
            )
        pending = self.pending_action_summary(manifest_id)
        if not pending["student"]:
            raise OneRosterError(
                "OR-GATE-NO-STUDENT-ACTIONS",
                "This manifest has no pending student enrollment work to release.",
            )
        if pending["nonstudent"]:
            raise OneRosterError(
                "OR-GATE-MANIFEST-NOT-READY",
                "Non-student work remains; replan before arming student release.",
            )
        return self.save_gate(gate_arm(manifest_id, manifest_hash, release_at))

    def open_gate(
        self,
        manifest_id: str,
        manifest_hash: str,
        current_manifest_hash: str,
        *,
        now: Optional[datetime] = None,
        allow_early: bool = False,
    ) -> StudentEnrollmentGate:
        manifest = self.get_manifest_header(manifest_id)
        persisted_current_hash = (
            current_manifest_hash
            if manifest.manifest_hash == current_manifest_hash
            else ""
        )
        gate = gate_open(
            self.get_gate(),
            manifest_id=manifest_id,
            manifest_hash=manifest_hash,
            current_manifest_hash=persisted_current_hash,
            now=now,
            allow_early=allow_early,
        )
        return self.save_gate(gate)

    def cleanup_expired(self, *, now: Optional[float] = None) -> int:
        cutoff = float(now if now is not None else time.time())
        with closing(self._conn()) as conn:
            rows = conn.execute(
                """
                SELECT id FROM imports
                WHERE domain = ? AND expires_at <= ? AND state != 'expired'
                """,
                (self.domain, cutoff),
            ).fetchall()
        removed = 0
        for row in rows:
            import_id = str(row["id"])
            _remove_tree(self.snapshot_dir(import_id), self.snapshots_root)
            with closing(self._conn()) as conn, conn:
                conn.execute(
                    """
                    UPDATE imports SET state = 'expired'
                    WHERE domain = ? AND id = ?
                    """,
                    (self.domain, import_id),
                )
            removed += 1
        return removed

    def purge_preview(self) -> PurgePreview:
        with closing(self._conn()) as conn:
            imports = conn.execute(
                """
                SELECT COUNT(*), MIN(imported_at), MAX(imported_at)
                FROM imports WHERE domain = ?
                """,
                (self.domain,),
            ).fetchone()
            manifests = int(
                conn.execute(
                    "SELECT COUNT(*) FROM manifests WHERE domain = ?",
                    (self.domain,),
                ).fetchone()[0]
            )
        return PurgePreview(
            snapshot_count=int(imports[0] or 0),
            manifest_count=manifests,
            retained_bytes=_tree_size(self.root),
            oldest_import_at=float(imports[1]) if imports[1] is not None else None,
            newest_import_at=float(imports[2]) if imports[2] is not None else None,
        )

    def purge_data(self, confirmation: str) -> PurgePreview:
        if confirmation != "OneRoster":
            raise OneRosterError(
                "OR-PURGE-CONFIRMATION",
                "Type OneRoster exactly to purge retained component data.",
            )
        preview = self.purge_preview()
        resolved = self.root.resolve()
        if resolved == Path(resolved.anchor) or resolved == Path.home().resolve():
            raise OneRosterError("OR-PURGE-PATH", "Refusing to purge an unsafe data path.")
        _remove_tree(self.root, self.root.parent)
        self.snapshots_root.mkdir(parents=True, exist_ok=True)
        self._init_state()
        self._restrict_state_perms()
        return preview

    def dashboard(self) -> DashboardStatus:
        history = self.history(1)
        with closing(self._conn()) as conn:
            import_count = int(
                conn.execute(
                    "SELECT COUNT(*) FROM imports WHERE domain = ?",
                    (self.domain,),
                ).fetchone()[0]
            )
        return DashboardStatus(
            latest=history[0] if history else None,
            import_count=import_count,
            threshold_profile_configured=self.get_threshold_profile().configured,
            gate_state=self.get_gate().state,
            retained_bytes=_tree_size(self.root),
        )

    def snapshot_dir(self, import_id: str) -> Path:
        _validate_id(import_id)
        return self.snapshots_root / import_id

    def normalized_path(self, import_id: str) -> Path:
        return self.snapshot_dir(import_id) / "normalized.db"

    def raw_path(self, import_id: str) -> Path:
        return self.snapshot_dir(import_id) / "source.zip"

    def _require_material(self, snapshot: OneRosterSnapshot) -> None:
        if snapshot.state is SnapshotState.EXPIRED or not self.normalized_path(snapshot.id).is_file():
            raise OneRosterError(
                "OR-IMPORT-EXPIRED",
                "The retained raw and normalized data for this import has expired.",
            )

    def _conn(self) -> sqlite3.Connection:
        _prepare_private_database(self.state_path)
        conn = sqlite3.connect(str(self.state_path), timeout=10.0)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=10000")
            conn.execute("PRAGMA foreign_keys=ON")
            self._restrict_state_perms()
            return conn
        except BaseException:
            conn.close()
            raise

    def _init_state(self) -> None:
        _secure_private_directory(self.root, create=True)
        _secure_private_directory(self.snapshots_root, create=True)
        _prepare_private_database(self.state_path)
        with closing(self._conn()) as conn, conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS imports (
                    id TEXT PRIMARY KEY, domain TEXT NOT NULL, filename TEXT NOT NULL,
                    source_sha256 TEXT NOT NULL, state TEXT NOT NULL,
                    package_mode TEXT NOT NULL, selected_session_id TEXT NOT NULL,
                    imported_at REAL NOT NULL, expires_at REAL NOT NULL,
                    counts_json TEXT NOT NULL, issue_count INTEGER NOT NULL,
                    blocking_issue_count INTEGER NOT NULL, accepted_at REAL NOT NULL,
                    course_name_template TEXT NOT NULL DEFAULT
                        '{course_title} \u2013 {class_code} ({school_year})',
                    scope_date TEXT NOT NULL DEFAULT '',
                    initial_scope_date TEXT NOT NULL DEFAULT '',
                    school_year_id TEXT NOT NULL DEFAULT '',
                    school_year_title TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS imports_domain_time
                    ON imports(domain, imported_at DESC);
                CREATE TABLE IF NOT EXISTS threshold_profiles (
                    domain TEXT PRIMARY KEY, profile_json TEXT NOT NULL, updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS threshold_overrides (
                    domain TEXT NOT NULL, import_id TEXT NOT NULL,
                    evaluation_hash TEXT NOT NULL, reason TEXT NOT NULL,
                    recorded_at REAL NOT NULL,
                    PRIMARY KEY(domain, import_id, evaluation_hash)
                );
                CREATE TABLE IF NOT EXISTS threshold_denials (
                    domain TEXT NOT NULL, import_id TEXT NOT NULL,
                    evaluation_hash TEXT NOT NULL, reason TEXT NOT NULL,
                    recorded_at REAL NOT NULL,
                    PRIMARY KEY(domain, import_id, evaluation_hash)
                );
                CREATE TABLE IF NOT EXISTS manifests (
                    id TEXT PRIMARY KEY, domain TEXT NOT NULL, import_id TEXT NOT NULL,
                    source_hash TEXT NOT NULL, config_hash TEXT NOT NULL,
                    live_hash TEXT NOT NULL, manifest_hash TEXT NOT NULL UNIQUE,
                    threshold_evaluation_hash TEXT NOT NULL, status TEXT NOT NULL,
                    created_at REAL NOT NULL, error TEXT NOT NULL,
                    plan_kind TEXT NOT NULL DEFAULT 'ordinary',
                    confirmed_at REAL NOT NULL DEFAULT 0,
                    threshold_evidence_json TEXT NOT NULL DEFAULT '{}',
                    exclusions_json TEXT NOT NULL DEFAULT '[]',
                    pilot_evidence_json TEXT NOT NULL DEFAULT '{}',
                    live_evidence_json TEXT NOT NULL DEFAULT '{}',
                    drift_report_json TEXT NOT NULL DEFAULT '[]',
                    prepared_live_hash TEXT NOT NULL DEFAULT '',
                    run_owner TEXT NOT NULL DEFAULT '',
                    run_pid INTEGER NOT NULL DEFAULT 0,
                    run_identity TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS manifest_actions (
                    manifest_id TEXT NOT NULL, action_id TEXT NOT NULL, kind TEXT NOT NULL,
                    subject TEXT NOT NULL, target TEXT NOT NULL, before_value TEXT NOT NULL,
                    after_value TEXT NOT NULL, status TEXT NOT NULL, detail TEXT NOT NULL,
                    PRIMARY KEY(manifest_id, action_id),
                    FOREIGN KEY(manifest_id) REFERENCES manifests(id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS manifest_actions_pending_kind
                    ON manifest_actions(manifest_id, status, kind);
                CREATE TABLE IF NOT EXISTS execution_runs (
                    id TEXT PRIMARY KEY,
                    manifest_id TEXT NOT NULL,
                    domain TEXT NOT NULL,
                    status TEXT NOT NULL,
                    phase TEXT NOT NULL,
                    started_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    last_heartbeat_at REAL NOT NULL,
                    current_batch_sequence INTEGER NOT NULL DEFAULT 0,
                    stop_requested INTEGER NOT NULL DEFAULT 0,
                    attempt_number INTEGER NOT NULL DEFAULT 1,
                    last_error_code TEXT NOT NULL DEFAULT '',
                    planning_seconds REAL NOT NULL DEFAULT 0,
                    directory_snapshot_seconds REAL NOT NULL DEFAULT 0,
                    classroom_snapshot_seconds REAL NOT NULL DEFAULT 0,
                    FOREIGN KEY(manifest_id) REFERENCES manifests(id) ON DELETE CASCADE
                );
                CREATE UNIQUE INDEX IF NOT EXISTS execution_runs_active_manifest
                    ON execution_runs(manifest_id)
                    WHERE status IN ('running', 'pause_requested', 'recovery_required');
                CREATE INDEX IF NOT EXISTS execution_runs_manifest_time
                    ON execution_runs(manifest_id, started_at DESC);
                CREATE TABLE IF NOT EXISTS execution_batches (
                    id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    manifest_id TEXT NOT NULL,
                    sequence_number INTEGER NOT NULL,
                    phase TEXT NOT NULL,
                    action_ids_hash TEXT NOT NULL,
                    action_count INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    prepared_at REAL NOT NULL,
                    started_at REAL NOT NULL DEFAULT 0,
                    completed_at REAL NOT NULL DEFAULT 0,
                    attempt_number INTEGER NOT NULL DEFAULT 1,
                    error_code TEXT NOT NULL DEFAULT '',
                    apply_seconds REAL NOT NULL DEFAULT 0,
                    verification_seconds REAL NOT NULL DEFAULT 0,
                    persistence_seconds REAL NOT NULL DEFAULT 0,
                    verification_attempts INTEGER NOT NULL DEFAULT 0,
                    worker_count INTEGER NOT NULL DEFAULT 5,
                    throttling_count INTEGER NOT NULL DEFAULT 0,
                    UNIQUE(run_id, sequence_number),
                    FOREIGN KEY(run_id) REFERENCES execution_runs(id) ON DELETE CASCADE,
                    FOREIGN KEY(manifest_id) REFERENCES manifests(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS execution_batch_actions (
                    batch_id TEXT NOT NULL,
                    action_id TEXT NOT NULL,
                    ordinal INTEGER NOT NULL,
                    PRIMARY KEY(batch_id, action_id),
                    UNIQUE(batch_id, ordinal),
                    FOREIGN KEY(batch_id) REFERENCES execution_batches(id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS execution_batches_run_status
                    ON execution_batches(run_id, status, sequence_number);
                CREATE TABLE IF NOT EXISTS student_gate (
                    domain TEXT PRIMARY KEY, state TEXT NOT NULL, timezone TEXT NOT NULL,
                    manifest_id TEXT NOT NULL, manifest_hash TEXT NOT NULL,
                    release_at TEXT NOT NULL, updated_at REAL NOT NULL,
                    hold_code TEXT NOT NULL, hold_detail TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS scope_readiness (
                    domain TEXT PRIMARY KEY, scope_hash TEXT NOT NULL,
                    verified_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS accepted_managed_aliases (
                    domain TEXT NOT NULL, import_id TEXT NOT NULL,
                    alias TEXT NOT NULL, accepted_at REAL NOT NULL,
                    PRIMARY KEY(domain, import_id, alias)
                );
                CREATE INDEX IF NOT EXISTS accepted_aliases_domain_time
                    ON accepted_managed_aliases(domain, accepted_at DESC, alias);
                """
            )
            _ensure_column(
                conn,
                "imports",
                "course_name_template",
                (
                    "TEXT NOT NULL DEFAULT "
                    "'{course_title} \u2013 {class_code} ({school_year})'"
                ),
            )
            _ensure_column(
                conn,
                "imports",
                "scope_date",
                "TEXT NOT NULL DEFAULT ''",
            )
            _ensure_column(
                conn,
                "imports",
                "initial_scope_date",
                "TEXT NOT NULL DEFAULT ''",
            )
            _ensure_column(
                conn,
                "imports",
                "school_year_id",
                "TEXT NOT NULL DEFAULT ''",
            )
            _ensure_column(
                conn,
                "imports",
                "school_year_title",
                "TEXT NOT NULL DEFAULT ''",
            )
            _ensure_column(
                conn,
                "manifests",
                "plan_kind",
                "TEXT NOT NULL DEFAULT 'ordinary'",
            )
            _ensure_column(
                conn,
                "manifests",
                "confirmed_at",
                "REAL NOT NULL DEFAULT 0",
            )
            _ensure_column(
                conn,
                "manifests",
                "threshold_evidence_json",
                "TEXT NOT NULL DEFAULT '{}'",
            )
            _ensure_column(
                conn,
                "manifests",
                "exclusions_json",
                "TEXT NOT NULL DEFAULT '[]'",
            )
            _ensure_column(
                conn,
                "manifests",
                "pilot_evidence_json",
                "TEXT NOT NULL DEFAULT '{}'",
            )
            _ensure_column(
                conn,
                "manifests",
                "live_evidence_json",
                "TEXT NOT NULL DEFAULT '{}'",
            )
            _ensure_column(
                conn,
                "manifests",
                "drift_report_json",
                "TEXT NOT NULL DEFAULT '[]'",
            )
            _ensure_column(
                conn,
                "manifests",
                "prepared_live_hash",
                "TEXT NOT NULL DEFAULT ''",
            )
            _ensure_column(
                conn,
                "manifests",
                "run_owner",
                "TEXT NOT NULL DEFAULT ''",
            )
            _ensure_column(
                conn,
                "manifests",
                "run_pid",
                "INTEGER NOT NULL DEFAULT 0",
            )
            _ensure_column(
                conn,
                "manifests",
                "run_identity",
                "TEXT NOT NULL DEFAULT ''",
            )
            for column, definition in (
                ("planning_seconds", "REAL NOT NULL DEFAULT 0"),
                ("directory_snapshot_seconds", "REAL NOT NULL DEFAULT 0"),
                ("classroom_snapshot_seconds", "REAL NOT NULL DEFAULT 0"),
            ):
                _ensure_column(conn, "execution_runs", column, definition)
            for column, definition in (
                ("apply_seconds", "REAL NOT NULL DEFAULT 0"),
                ("verification_seconds", "REAL NOT NULL DEFAULT 0"),
                ("persistence_seconds", "REAL NOT NULL DEFAULT 0"),
                ("verification_attempts", "INTEGER NOT NULL DEFAULT 0"),
                ("worker_count", "INTEGER NOT NULL DEFAULT 5"),
                ("throttling_count", "INTEGER NOT NULL DEFAULT 0"),
            ):
                _ensure_column(conn, "execution_batches", column, definition)

    def _restrict_state_perms(self) -> None:
        _chmod(self.root, 0o700)
        _chmod(self.snapshots_root, 0o700)
        _chmod(self.state_path, 0o600)
        for companion in (
            Path(f"{self.state_path}-wal"),
            Path(f"{self.state_path}-shm"),
        ):
            try:
                _chmod(companion, 0o600)
            except FileNotFoundError:
                # SQLite owns companion lifetimes and may remove one while it
                # is being hardened. Main-state and all other failures remain
                # fail-closed.
                continue


def _snapshot_conn(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


def _execution_run_from_row(row: sqlite3.Row) -> ExecutionRun:
    return ExecutionRun(
        id=str(row["id"]),
        manifest_id=str(row["manifest_id"]),
        status=str(row["status"]),
        phase=str(row["phase"]),
        started_at=float(row["started_at"]),
        updated_at=float(row["updated_at"]),
        last_heartbeat_at=float(row["last_heartbeat_at"]),
        current_batch_sequence=int(row["current_batch_sequence"] or 0),
        stop_requested=bool(row["stop_requested"]),
        attempt_number=int(row["attempt_number"] or 1),
        last_error_code=str(row["last_error_code"] or ""),
        planning_seconds=float(row["planning_seconds"] or 0),
        directory_snapshot_seconds=float(row["directory_snapshot_seconds"] or 0),
        classroom_snapshot_seconds=float(row["classroom_snapshot_seconds"] or 0),
    )


def _execution_batch_from_row(
    row: sqlite3.Row,
    action_ids: Sequence[str] = (),
) -> ExecutionBatch:
    return ExecutionBatch(
        id=str(row["id"]),
        run_id=str(row["run_id"]),
        manifest_id=str(row["manifest_id"]),
        sequence_number=int(row["sequence_number"]),
        phase=str(row["phase"]),
        action_ids_hash=str(row["action_ids_hash"]),
        action_count=int(row["action_count"]),
        status=str(row["status"]),
        prepared_at=float(row["prepared_at"]),
        started_at=float(row["started_at"] or 0),
        completed_at=float(row["completed_at"] or 0),
        attempt_number=int(row["attempt_number"] or 1),
        error_code=str(row["error_code"] or ""),
        apply_seconds=float(row["apply_seconds"] or 0),
        verification_seconds=float(row["verification_seconds"] or 0),
        persistence_seconds=float(row["persistence_seconds"] or 0),
        verification_attempts=int(row["verification_attempts"] or 0),
        worker_count=int(row["worker_count"] or 5),
        throttling_count=int(row["throttling_count"] or 0),
        action_ids=tuple(action_ids),
    )


def _snapshot_write_conn(path: Path) -> sqlite3.Connection:
    _prepare_private_database(path)
    conn = sqlite3.connect(str(path), timeout=10.0)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=10000")
        _secure_sqlite_files(path)
        return conn
    except BaseException:
        conn.close()
        raise


def _managed_aliases_from_snapshot(path: Path, domain: str) -> tuple[str, ...]:
    with closing(_snapshot_conn(path)) as conn:
        rows = conn.execute(
            """
            SELECT alias FROM course_plans
            WHERE domain = ? AND selected = 1 AND ready = 1
            ORDER BY alias COLLATE NOCASE
            """,
            (domain,),
        ).fetchall()
    return tuple(
        alias
        for row in rows
        if (alias := str(row["alias"] or "")).startswith("Section_")
    )


def _snapshot_from_row(row: sqlite3.Row) -> OneRosterSnapshot:
    return OneRosterSnapshot(
        id=str(row["id"]),
        domain=str(row["domain"]),
        filename=str(row["filename"]),
        source_sha256=str(row["source_sha256"]),
        state=SnapshotState(str(row["state"])),
        package_mode=str(row["package_mode"]),
        selected_session_id=str(row["selected_session_id"]),
        imported_at=float(row["imported_at"]),
        expires_at=float(row["expires_at"]),
        counts=SnapshotCounts.from_mapping(json.loads(str(row["counts_json"]))),
        issue_count=int(row["issue_count"]),
        blocking_issue_count=int(row["blocking_issue_count"]),
        course_name_template=str(
            row["course_name_template"] or DEFAULT_COURSE_NAME_TEMPLATE
        ),
        scope_date=str(row["scope_date"] or ""),
        initial_scope_date=str(row["initial_scope_date"] or ""),
        school_year_id=str(row["school_year_id"] or ""),
        school_year_title=str(row["school_year_title"] or ""),
    )


def _preview_item(kind: str, row: sqlite3.Row) -> Mapping[str, Any]:
    item = {key: row[key] for key in row.keys() if key != "_entity_key"}
    if kind in {"courses", "excluded"}:
        item["quarantine_codes"] = tuple(json.loads(str(item.pop("quarantine_json") or "[]")))
        item["ready"] = bool(item["ready"])
        item["selected"] = bool(item["selected"])
    if kind == "issues":
        item["blocking"] = bool(item["blocking"])
    if "is_primary" in item:
        item["is_primary"] = bool(item["is_primary"])
    if "in_scope" in item:
        item["in_scope"] = bool(item["in_scope"])
    return item


def _export_query(kind: str) -> Optional[tuple[tuple[str, ...], str]]:
    queries = {
        "courses": (
            ("alias", "name", "section", "room", "teacher", "status"),
            """
            SELECT alias, name, section, room, owner_email AS teacher,
                   'PROVISIONED' AS status
            FROM course_plans
            WHERE domain = ? AND selected = 1 AND ready = 1
            ORDER BY alias
            """,
        ),
        "teachers": (
            ("alias", "teacher"),
            """
            SELECT DISTINCT p.alias, u.email AS teacher
            FROM course_plans p JOIN enrollments e
              ON e.domain = p.domain AND e.class_id = p.class_id
            JOIN users u ON u.domain = e.domain AND u.sourced_id = e.user_id
            WHERE p.domain = ? AND p.selected = 1 AND p.ready = 1
              AND e.status != 'tobedeleted' AND e.in_scope = 1
              AND e.role = 'teacher'
              AND trim(u.email) != ''
            ORDER BY p.alias, u.email
            """,
        ),
        "students": (
            ("alias", "email"),
            """
            SELECT DISTINCT p.alias, u.email
            FROM course_plans p JOIN enrollments e
              ON e.domain = p.domain AND e.class_id = p.class_id
            JOIN users u ON u.domain = e.domain AND u.sourced_id = e.user_id
            WHERE p.domain = ? AND p.selected = 1 AND p.ready = 1
              AND e.status != 'tobedeleted' AND e.in_scope = 1
              AND e.role = 'student'
              AND trim(u.email) != ''
            ORDER BY p.alias, u.email
            """,
        ),
        "courses_needs_teacher": (
            ("alias", "name", "reason"),
            """
            SELECT alias, name, quarantine_json AS reason
            FROM course_plans
            WHERE domain = ? AND selected = 1 AND ready = 0
            ORDER BY alias
            """,
        ),
    }
    return queries.get(kind)


def _csv_line(values: Sequence[str]) -> str:
    buffer = io.StringIO(newline="")
    csv.writer(buffer, lineterminator="\n").writerow(values)
    return buffer.getvalue()


def _write_text(stream: BinaryIO | TextIO, value: str) -> None:
    try:
        stream.write(value)  # type: ignore[arg-type]
    except TypeError:
        stream.write(value.encode("utf-8"))  # type: ignore[arg-type]


def _manifest_from_rows(
    row: sqlite3.Row,
    actions: Sequence[sqlite3.Row],
) -> ClassroomImportManifest:
    return ClassroomImportManifest(
        id=str(row["id"]),
        domain=str(row["domain"]),
        import_id=str(row["import_id"]),
        source_hash=str(row["source_hash"]),
        config_hash=str(row["config_hash"]),
        live_hash=str(row["live_hash"]),
        manifest_hash=str(row["manifest_hash"]),
        threshold_evaluation_hash=str(row["threshold_evaluation_hash"]),
        status=str(row["status"]),
        created_at=float(row["created_at"]),
        actions=tuple(_action_from_row(item) for item in actions),
        prepared_live_hash=str(row["prepared_live_hash"] or ""),
        error=str(row["error"]),
        plan_kind=str(row["plan_kind"] or "ordinary"),
        confirmed_at=float(row["confirmed_at"] or 0),
        threshold_evidence=_json_mapping(row["threshold_evidence_json"]),
        exclusions=tuple(
            _issue_from_record(item)
            for item in _json_sequence(row["exclusions_json"])
        ),
        pilot_evidence=_json_mapping(row["pilot_evidence_json"]),
        live_evidence=_json_mapping(row["live_evidence_json"]),
        drift_report=tuple(
            item for item in _json_sequence(row["drift_report_json"])
            if isinstance(item, Mapping)
        ),
    )


def _action_from_row(row: sqlite3.Row) -> ImportAction:
    return ImportAction(
        id=str(row["action_id"]),
        kind=str(row["kind"]),
        subject=str(row["subject"]),
        target=str(row["target"]),
        before=str(row["before_value"]),
        after=str(row["after_value"]),
        status=str(row["status"]),
        detail=str(row["detail"]),
    )


def action_sequence_hash(actions: Iterable[ImportAction]) -> tuple[str, int]:
    """Hash an ordered action stream without constructing a combined payload."""

    digest = hashlib.sha256()
    count = 0
    for action in actions:
        payload = json.dumps(
            action.basis_dict(),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
        digest.update(len(payload).to_bytes(8, "big"))
        digest.update(payload)
        count += 1
    return digest.hexdigest(), count


def _gate_from_row(row: sqlite3.Row) -> StudentEnrollmentGate:
    return StudentEnrollmentGate(
        state=GateState(str(row["state"])),
        timezone=str(row["timezone"]),
        manifest_id=str(row["manifest_id"]),
        manifest_hash=str(row["manifest_hash"]),
        release_at=str(row["release_at"]),
        updated_at=float(row["updated_at"]),
        hold_code=str(row["hold_code"]),
        hold_detail=str(row["hold_detail"]),
    )


def _normalize_domain(domain: str) -> str:
    value = (domain or "").strip().casefold()
    if not value or "/" in value or "\\" in value or "\x00" in value:
        raise ValueError("A valid Workspace domain is required for OneRoster.")
    return value


def _limited_actions(actions: Sequence[ImportAction]) -> tuple[ImportAction, ...]:
    """Return additions only, including activation solely for newly created courses."""

    created = {
        action.subject.casefold()
        for action in actions
        if action.kind == "course_create"
    }
    return tuple(
        action
        for action in actions
        if action.kind in {"course_create", "teacher_add", "student_add"}
        or (
            action.kind == "course_activate"
            and action.subject.casefold() in created
        )
    )


def _validate_digest(value: str) -> str:
    digest = str(value or "").strip().casefold()
    if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
        raise ValueError("Expected one SHA-256 digest.")
    return digest


def _bounded_json_mapping(
    value: Mapping[str, Any],
    *,
    maximum_bytes: int = 256 * 1024,
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("Manifest evidence must be a mapping.")
    try:
        encoded = json.dumps(
            dict(value),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            default=str,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError("Manifest evidence is not JSON serializable.") from exc
    if len(encoded) > maximum_bytes:
        raise ValueError("Manifest evidence exceeds its protected storage limit.")
    result = json.loads(encoded.decode("utf-8"))
    return dict(result) if isinstance(result, dict) else {}


def _bounded_live_evidence(value: Mapping[str, Any]) -> dict[str, Any]:
    """Retain deterministic evidence for at most 500 affected managed courses."""

    selected = {
        str(alias)[:512]: record
        for alias, record in sorted(value.items(), key=lambda item: str(item[0]).casefold())[:500]
    }
    if len(value) > len(selected):
        selected["__truncated__"] = {
            "total_affected_courses": len(value),
            "retained_courses": len(selected),
        }
    return _bounded_json_mapping(selected, maximum_bytes=4 * 1024 * 1024)


def _issue_record(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        raw = dict(value)
    else:
        raw = {
            name: getattr(value, name)
            for name in (
                "code",
                "severity",
                "message",
                "entity_kind",
                "source_id",
                "row_number",
                "blocking",
            )
            if hasattr(value, name)
        }
    severity = raw.get("severity", IssueSeverity.ERROR)
    if hasattr(severity, "value"):
        severity = severity.value
    return {
        "code": str(raw.get("code", ""))[:128],
        "severity": str(severity or IssueSeverity.ERROR.value),
        "message": str(raw.get("message", ""))[:1000],
        "entity_kind": str(raw.get("entity_kind", ""))[:128],
        "source_id": str(raw.get("source_id", ""))[:256],
        "row_number": (
            int(raw["row_number"])
            if raw.get("row_number") not in (None, "")
            else None
        ),
        "blocking": bool(raw.get("blocking", True)),
    }


def _json_mapping(value: Any) -> dict[str, Any]:
    try:
        parsed = json.loads(str(value or "{}"))
    except (TypeError, ValueError):
        return {}
    return dict(parsed) if isinstance(parsed, dict) else {}


def _json_sequence(value: Any) -> tuple[Mapping[str, Any], ...]:
    try:
        parsed = json.loads(str(value or "[]"))
    except (TypeError, ValueError):
        return ()
    if not isinstance(parsed, list):
        return ()
    return tuple(item for item in parsed if isinstance(item, dict))


def _issue_from_record(value: Mapping[str, Any]) -> Any:
    try:
        severity = IssueSeverity(str(value.get("severity", IssueSeverity.ERROR.value)))
    except ValueError:
        severity = IssueSeverity.ERROR
    row_number = value.get("row_number")
    return ImportIssue(
        code=str(value.get("code", "")),
        severity=severity,
        message=str(value.get("message", "")),
        entity_kind=str(value.get("entity_kind", "")),
        source_id=str(value.get("source_id", "")),
        row_number=int(row_number) if row_number not in (None, "") else None,
        blocking=bool(value.get("blocking", True)),
    )


def _validate_id(value: str) -> None:
    text = str(value or "")
    if len(text) != 32 or any(character not in "0123456789abcdef" for character in text):
        raise KeyError("OneRoster object not found.")


def _ensure_column(
    conn: sqlite3.Connection,
    table: str,
    column: str,
    definition: str,
) -> None:
    columns = {
        str(row["name"])
        for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
    }
    if column not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def _cursor_scope(import_id: str, kind: str, query: str) -> str:
    return hashlib.sha256(f"{import_id}\0{kind}\0{query}".encode()).hexdigest()[:16]


def _encode_cursor(last_rowid: int, import_id: str, kind: str, query: str) -> str:
    raw = json.dumps(
        {
            "v": 2,
            "last_rowid": max(0, int(last_rowid)),
            "scope": _cursor_scope(import_id, kind, query),
        },
        separators=(",", ":"),
    ).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode_cursor(
    cursor: Optional[str],
    import_id: str,
    kind: str,
    query: str,
) -> int:
    if not cursor:
        return 0
    try:
        value = str(cursor)
        data = json.loads(
            base64.urlsafe_b64decode((value + "=" * (-len(value) % 4)).encode())
        )
        if (
            data.get("v") != 2
            or data.get("scope") != _cursor_scope(import_id, kind, query)
        ):
            return 0
        return max(0, int(data.get("last_rowid", 0)))
    except (ValueError, TypeError, json.JSONDecodeError):
        return 0


def _tree_size(root: Path) -> int:
    if not root.exists():
        return 0
    total = 0
    for path in root.rglob("*"):
        try:
            if path.is_file() and not path.is_symlink():
                total += path.stat().st_size
        except OSError:
            continue
    return total


def _remove_tree(target: Path, allowed_parent: Path) -> None:
    resolved_target = target.resolve()
    resolved_parent = allowed_parent.resolve()
    if resolved_target == resolved_parent or resolved_parent not in resolved_target.parents:
        raise OneRosterError("OR-PURGE-PATH", "Refusing to remove an unsafe data path.")
    if target.exists():
        shutil.rmtree(target)


def _chmod(path: Path, mode: int) -> None:
    path = Path(path)
    metadata = path.lstat()
    expected_type = stat.S_ISDIR if mode == 0o700 else stat.S_ISREG
    if mode not in {0o600, 0o700}:
        raise ValueError("OneRoster persistence mode must be owner-only.")
    if path.is_symlink() or not expected_type(metadata.st_mode):
        raise PermissionError("OneRoster persistence path has an unsafe type.")
    os.chmod(path, mode)
    _verify_owner_only(path, expected_mode=mode, directory=mode == 0o700)


def _prepare_private_database(path: Path) -> None:
    """Create an owner-only SQLite file before any roster PII can be written."""

    path = Path(path)
    _secure_private_directory(path.parent, create=True)
    flags = os.O_RDWR | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(str(path), flags, 0o600)
    except FileExistsError:
        _secure_private_file(path)
        return
    try:
        fchmod = getattr(os, "fchmod", None)
        if callable(fchmod):
            fchmod(descriptor, 0o600)
    finally:
        os.close(descriptor)
    _secure_private_file(path)


def _secure_sqlite_files(path: Path) -> None:
    _secure_private_directory(path.parent)
    _secure_private_file(path)
    for companion in (
        Path(f"{path}-wal"),
        Path(f"{path}-shm"),
    ):
        try:
            _secure_private_file(companion)
        except FileNotFoundError:
            # SQLite can delete a WAL/SHM file after it was observed. Missing
            # companions are safe; permission, symlink, and type errors are not.
            continue


def _secure_private_directory(path: Path, *, create: bool = False) -> None:
    path = Path(path)
    if create:
        path.mkdir(parents=True, exist_ok=True)
    _chmod(path, 0o700)


def _secure_private_file(path: Path) -> None:
    _chmod(Path(path), 0o600)


def _verify_owner_only(path: Path, *, expected_mode: int, directory: bool) -> None:
    metadata = path.lstat()
    expected_type = stat.S_ISDIR if directory else stat.S_ISREG
    if path.is_symlink() or not expected_type(metadata.st_mode):
        raise PermissionError("OneRoster persistence changed type unexpectedly.")
    getuid = getattr(os, "getuid", None)
    if callable(getuid) and int(metadata.st_uid) != int(getuid()):
        raise PermissionError("OneRoster persistence is not owned by this user.")
    if os.name == "posix" and stat.S_IMODE(metadata.st_mode) != expected_mode:
        raise PermissionError("OneRoster persistence is not owner-only.")
