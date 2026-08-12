"""Optional OneRoster Classroom Import Studio web routes.

Opening these routes only reads local component/import state.  Google and GAM work is
reserved for explicitly approved engine jobs; the web adapter never falls back to the
ordinary Classroom connector when the optional service is unavailable.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import re
import tempfile
import zipfile
from collections import Counter, deque
from datetime import date, datetime, timedelta, timezone
from typing import Annotated, Any, AsyncIterator, Iterable

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, Response, StreamingResponse

from ..activity import ADMIN_ACTIVITY_BUSY_MESSAGE, try_acquire_admin_activity
from ..server import TEMPLATES
from .components import component_context

router = APIRouter(prefix="/classroom/imports")

_MAX_UPLOAD_BYTES = 250 * 1024 * 1024
_MAX_FOLDER_FILES = 32
_FOLDER_ALLOWED_FILES = frozenset(
    {
        "manifest.csv",
        "academicsessions.csv",
        "classes.csv",
        "courses.csv",
        "enrollments.csv",
        "orgs.csv",
        "users.csv",
        "categories.csv",
        "classresources.csv",
        "courseresources.csv",
        "demographics.csv",
        "lineitems.csv",
        "resources.csv",
        "results.csv",
    }
)
_FOLDER_IGNORED_FILES = frozenset({".ds_store"})
_IMPORT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_MANIFEST_HASH = re.compile(r"^[0-9a-f]{64}$")
_PREVIEW_KINDS = {
    "courses",
    "teachers",
    "students",
    "enrollments",
    "sessions",
    "issues",
    "excluded",
}
_PREFERRED_PREVIEW_COLUMNS = {
    "courses": (
        "class_id",
        "alias",
        "name",
        "scope_state",
        "term_start",
        "days_until_start",
        "creation_cutoff",
        "changed_since_upload",
        "selected",
        "ready",
    ),
}
_EXPORT_KINDS = {
    "courses",
    "teachers",
    "students",
    "courses_needs_teacher",
}
_GATE_STATES = {"CLOSED", "ARMED", "OPEN"}
_MANIFEST_TERMINAL_STATES = {
    "completed",
    "partial",
    "failed",
    "interrupted",
    "paused",
    "recovery_required",
    "stale",
}
_THRESHOLD_ACTIONS = (
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
_THRESHOLD_KEYS = {
    f"{action}_{measure}"
    for action in _THRESHOLD_ACTIONS
    for measure in ("count", "percent")
}
_CHECKED_VALUES = {"1", "true", "yes", "on"}
_DEFAULT_COURSE_NAME_TEMPLATE = (
    "{course_title} \u2013 {class_code} ({school_year})"
)
_COURSE_NAME_PRESETS = {
    "default": _DEFAULT_COURSE_NAME_TEMPLATE,
    "course_and_class": (
        "{course_title} \u2013 {class_title}[[ ({school_year})]]"
    ),
    "course_and_period": (
        "{course_title}[[ \u2013 {class_code}]][[ ({school_year})]]"
    ),
    "class_title": "{class_title}",
}


class _LocalUploadError(ValueError):
    """Safe validation failure produced before the component service runs."""

    def __init__(self, code: str, message: str) -> None:
        self.code = str(code)
        self.message = str(message)
        super().__init__(message)


class _BoundedUploadStream:
    """Read-through wrapper that enforces the web upload cap while streaming."""

    def __init__(self, source: Any, maximum: int) -> None:
        self._source = source
        self._maximum = int(maximum)
        self._read = 0

    def read(self, size: int = -1) -> bytes:
        remaining = self._maximum - self._read
        request_size = remaining + 1 if size < 0 else min(size, remaining + 1)
        data = self._source.read(request_size)
        self._read += len(data)
        if self._read > self._maximum:
            raise _LocalUploadError(
                "OR-ZIP-SIZE",
                "The package exceeds the 250 MB upload limit.",
            )
        return data


def _folder_upload_archive(
    uploads: Iterable[UploadFile],
) -> tuple[Any, str]:
    """Build a flat, stored ZIP from a browser-selected OneRoster folder."""

    selected = tuple(uploads)
    if not selected:
        raise _LocalUploadError(
            "OR-FOLDER-EMPTY",
            "Choose a folder containing the OneRoster CSV files.",
        )
    if len(selected) > _MAX_FOLDER_FILES:
        raise _LocalUploadError(
            "OR-FOLDER-FILE-LIMIT",
            f"The selected folder contains more than {_MAX_FOLDER_FILES} files.",
        )

    archive = tempfile.SpooledTemporaryFile(max_size=8 * 1024 * 1024, mode="w+b")
    names: set[str] = set()
    roots: set[str] = set()
    total = 0
    try:
        with zipfile.ZipFile(
            archive,
            "w",
            compression=zipfile.ZIP_STORED,
            allowZip64=True,
        ) as package:
            for upload in selected:
                supplied_name = str(upload.filename or "").replace("\\", "/")
                parts = tuple(
                    part for part in supplied_name.split("/") if part
                )
                if (
                    not parts
                    or any(part in {".", ".."} for part in parts)
                    or "\x00" in supplied_name
                ):
                    raise _LocalUploadError(
                        "OR-FOLDER-PATH",
                        "The selected folder contains an invalid file path.",
                    )
                if len(parts) > 1:
                    roots.add(parts[0])
                member_name = parts[-1]
                folded = member_name.casefold()
                if folded in _FOLDER_IGNORED_FILES:
                    continue
                if folded not in _FOLDER_ALLOWED_FILES:
                    raise _LocalUploadError(
                        "OR-FOLDER-UNEXPECTED-FILE",
                        f"The selected folder contains an unsupported file: "
                        f"{member_name}.",
                    )
                if folded in names:
                    raise _LocalUploadError(
                        "OR-FOLDER-DUPLICATE-FILE",
                        f"The selected folder contains duplicate OneRoster files: "
                        f"{member_name}.",
                    )
                names.add(folded)
                with package.open(member_name, "w", force_zip64=True) as target:
                    while True:
                        chunk = upload.file.read(1024 * 1024)
                        if not chunk:
                            break
                        if not isinstance(chunk, (bytes, bytearray)):
                            raise TypeError(
                                "Folder uploads must contain binary file streams."
                            )
                        total += len(chunk)
                        if total > _MAX_UPLOAD_BYTES:
                            raise _LocalUploadError(
                                "OR-FOLDER-SIZE",
                                "The selected folder exceeds the 250 MB upload limit.",
                            )
                        target.write(chunk)
        if not names:
            raise _LocalUploadError(
                "OR-FOLDER-EMPTY",
                "The selected folder does not contain OneRoster CSV files.",
            )
        archive.seek(0, 2)
        if archive.tell() > _MAX_UPLOAD_BYTES:
            raise _LocalUploadError(
                "OR-FOLDER-SIZE",
                "The selected folder exceeds the 250 MB upload limit.",
            )
        archive.seek(0)
        root_name = next(iter(roots)) if len(roots) == 1 else "OneRoster folder"
        safe_root = re.sub(r"[^A-Za-z0-9 ._-]+", "_", root_name).strip(" ._-")
        filename = f"{(safe_root or 'OneRoster folder')[:80]}.zip"
        return archive, filename
    except BaseException:
        archive.close()
        raise


def _record(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return dict(value)
    if hasattr(value, "__dataclass_fields__"):
        return {
            name: getattr(value, name)
            for name in value.__dataclass_fields__
            if not name.startswith("_")
        }
    data = getattr(value, "__dict__", None)
    return dict(data) if isinstance(data, dict) else {}


def _records(value: Any) -> list[dict[str, Any]]:
    if value is None:
        return []
    if isinstance(value, dict):
        for key in ("items", "rows", "imports", "history"):
            if key in value:
                value = value[key]
                break
    if not isinstance(value, (list, tuple)):
        try:
            value = list(value)
        except TypeError:
            return []
    return [_record(item) for item in value]


def _bounded_preview_rows(
    rows: Iterable[dict[str, Any]],
    columns: Iterable[str],
    maximum_chars: int,
) -> list[dict[str, Any]]:
    bounded: list[dict[str, Any]] = []
    maximum = max(8, int(maximum_chars))
    selected = tuple(columns)
    for row in rows:
        item: dict[str, Any] = {}
        sourced_id = str(row.get("sourced_id", "") or "")
        if sourced_id and len(sourced_id) <= 128:
            item["_session_id"] = sourced_id
        for column in selected:
            value = row.get(column, "")
            if isinstance(value, (tuple, list)):
                text = ", ".join(str(part) for part in value)
            elif value is None:
                text = ""
            else:
                text = str(value)
            item[column] = (
                text
                if len(text) <= maximum
                else f"{text[: maximum - 1]}…"
            )
        bounded.append(item)
    return bounded


def _enum_value(value: Any) -> Any:
    return value.value if hasattr(value, "value") else value


def _snapshot_record(value: Any) -> dict[str, Any]:
    result = _record(value)
    if not result:
        return result
    result["state"] = _enum_value(result.get("state", ""))
    counts = result.get("counts")
    to_dict = getattr(counts, "to_dict", None)
    if callable(to_dict):
        counts = to_dict()
    elif counts is not None and not isinstance(counts, dict):
        counts = _record(counts)
    result["counts"] = counts or {}
    if "ready_for_apply" not in result:
        result["ready_for_apply"] = bool(getattr(value, "ready_for_apply", False))
    template = str(
        result.get("course_name_template", "")
        or _DEFAULT_COURSE_NAME_TEMPLATE
    )
    result["course_name_template"] = template
    result["course_name_choice"] = next(
        (
            name
            for name, preset in _COURSE_NAME_PRESETS.items()
            if preset == template
        ),
        "custom",
    )
    result["custom_course_name_template"] = (
        template if result["course_name_choice"] == "custom" else ""
    )
    scope_date = str(result.get("scope_date", "") or "")
    initial_scope_date = str(result.get("initial_scope_date", "") or "")
    try:
        result["scope_window_end"] = (
            date.fromisoformat(scope_date) + timedelta(days=31)
        ).isoformat()
    except ValueError:
        result["scope_window_end"] = ""
    result["scope_changed_since_upload"] = bool(
        scope_date and initial_scope_date and scope_date != initial_scope_date
    )
    return result


def _action_record(value: Any) -> dict[str, Any]:
    result = _record(value)
    limits = {
        "id": 64,
        "kind": 64,
        "subject": 254,
        "target": 254,
        "before": 256,
        "after": 256,
        "status": 32,
        "detail": 256,
    }
    bounded = {
        key: str(_enum_value(result.get(key, "")) or "")[:maximum]
        for key, maximum in limits.items()
    }
    bounded["status"] = bounded["status"] or "pending"
    return bounded


def _evaluation_record(value: Any) -> dict[str, Any]:
    result = _record(value)
    result["held"] = bool(result.get("held", False))
    result["limited_import"] = bool(result.get("limited_import", False))
    result["blackout"] = bool(result.get("blackout", False))
    result["counts"] = dict(result.get("counts", {}) or {})
    result["baselines"] = dict(result.get("baselines", {}) or {})
    result["breaches"] = [
        _record(item) for item in (result.get("breaches", ()) or ())
    ][:50]
    return result


def _planning_record(value: Any) -> dict[str, Any]:
    result = _record(value)
    evaluation = _evaluation_record(result.get("threshold_evaluation", {}))
    hasher = _start_planning_confirmation_hash()
    first_action = True
    action_counts: Counter[str] = Counter()
    action_groups: dict[str, list[dict[str, Any]]] = {}
    action_totals: dict[str, int] = {}

    for group in ("actions", "archive_actions", "ownership_actions"):
        preview: list[dict[str, Any]] = []
        total = 0
        for raw_action in result.get(group, ()) or ():
            action = _action_record(raw_action)
            total += 1
            action_counts[action["kind"]] += 1
            if len(preview) < 50:
                preview.append(action)
            first_action = _update_planning_confirmation_hash(
                hasher,
                action,
                first_action=first_action,
            )
        action_groups[group] = preview
        action_totals[group] = total

    issues: list[dict[str, Any]] = []
    issue_total = 0
    for raw_issue in result.get("issues", ()) or ():
        issue_total += 1
        if len(issues) < 50:
            issues.append(_record(raw_issue))

    result.update(
        actions=action_groups["actions"],
        action_total=action_totals["actions"],
        archive_actions=action_groups["archive_actions"],
        archive_total=action_totals["archive_actions"],
        ownership_actions=action_groups["ownership_actions"],
        ownership_total=action_totals["ownership_actions"],
        action_counts=dict(sorted(action_counts.items())),
        issues=issues,
        issue_total=issue_total,
        threshold_evaluation=evaluation,
    )
    result["confirmation_hash"] = _finish_planning_confirmation_hash(
        hasher,
        result,
        evaluation,
    )
    return result


def _planning_confirmation_hash(value: Any) -> str:
    """Hash the exact operator-visible live plan without its evaluation timestamp."""

    record = _record(value)
    evaluation = _evaluation_record(record.get("threshold_evaluation", {}))
    hasher = _start_planning_confirmation_hash()
    first_action = True
    for group in ("actions", "archive_actions", "ownership_actions"):
        for raw_action in record.get(group, ()) or ():
            first_action = _update_planning_confirmation_hash(
                hasher,
                _action_record(raw_action),
                first_action=first_action,
            )
    return _finish_planning_confirmation_hash(hasher, record, evaluation)


async def _planning_confirmation_hash_async(value: Any) -> str:
    """Keep district-sized confirmation hashing off the server event loop."""

    return await asyncio.to_thread(_planning_confirmation_hash, value)


def _canonical_json_bytes(value: Any) -> bytes:
    """Encode one bounded confirmation-hash fragment deterministically."""

    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    ).encode("utf-8")


def _start_planning_confirmation_hash() -> Any:
    hasher = hashlib.sha256()
    hasher.update(b'{"actions":[')
    return hasher


def _update_planning_confirmation_hash(
    hasher: Any,
    action: dict[str, Any],
    *,
    first_action: bool,
) -> bool:
    if not first_action:
        hasher.update(b",")
    hasher.update(
        _canonical_json_bytes(
            {
                key: action.get(key, "")
                for key in ("id", "kind", "subject", "target", "before", "after")
            }
        )
    )
    return False


def _finish_planning_confirmation_hash(
    hasher: Any,
    record: dict[str, Any],
    evaluation: dict[str, Any],
) -> str:
    """Finish the legacy-compatible canonical object without buffering actions."""

    hasher.update(b"]")
    scalar_members = (
        ("config_hash", str(record.get("config_hash", "") or "")),
        ("import_id", str(record.get("import_id", "") or "")),
        ("limited_import", bool(record.get("limited_import", False))),
        ("live_hash", str(record.get("live_hash", "") or "")),
        ("source_hash", str(record.get("source_hash", "") or "")),
        (
            "threshold",
            {
                key: evaluation.get(key)
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
        ),
    )
    for key, member in scalar_members:
        hasher.update(b",")
        hasher.update(_canonical_json_bytes(key))
        hasher.update(b":")
        hasher.update(_canonical_json_bytes(member))
    hasher.update(b"}")
    return hasher.hexdigest()


def _manifest_record(
    value: Any,
    *,
    offset: int = 0,
    limit: int = 50,
) -> dict[str, Any]:
    page = _record(value)
    nested = page.get("manifest")
    is_page = nested is not None and "total" in page
    result = _record(nested) if is_page else page
    page_size = max(1, min(int(limit or 50), 50))
    if is_page:
        actions = [
            _action_record(item)
            for item in (result.get("actions", ()) or ())
        ][:page_size]
        total = max(0, int(page.get("total", 0) or 0))
        start = max(0, int(page.get("offset", 0) or 0))
        page_size = max(1, min(int(page.get("limit", page_size) or page_size), 50))
        complete_count = max(0, int(page.get("complete_count", 0) or 0))
        pending_count = max(0, int(page.get("pending_count", 0) or 0))
        action_counts = {
            str(key)[:64]: max(0, int(item or 0))
            for key, item in dict(page.get("action_counts", {}) or {}).items()
        }
    else:
        start = max(0, int(offset or 0))
        actions = []
        tail: deque[dict[str, Any]] = deque(maxlen=page_size)
        total = complete_count = pending_count = 0
        counts: Counter[str] = Counter()
        raw_actions = result.get("actions", ()) or ()
        for index, raw_action in enumerate(raw_actions):
            action = _action_record(raw_action)
            total += 1
            complete_count += action["status"] in {
                "applied",
                "failed",
                "skipped",
            }
            pending_count += action["status"] == "pending"
            counts[action["kind"]] += 1
            tail.append(action)
            if start <= index < start + page_size:
                actions.append(action)
        action_counts = dict(sorted(counts.items()))
        if total and start >= total:
            # Non-page callers are a compatibility path. Re-scan only an
            # invalid requested page; ordinary create responses always use 0.
            actions = list(tail)
            start = max(0, total - len(actions))
    result.update(
        status=str(_enum_value(result.get("status", "planned")) or "planned"),
        plan_kind=str(result.get("plan_kind", "ordinary") or "ordinary"),
        actions=actions,
        action_total=total,
        action_offset=start,
        action_limit=page_size,
        previous_offset=max(0, start - page_size) if start else None,
        next_offset=start + page_size if start + page_size < total else None,
        complete_count=complete_count,
        pending_count=pending_count,
        action_counts=action_counts,
        student_action_count=(
            action_counts.get("student_add", 0)
            + action_counts.get("student_remove", 0)
        ),
        threshold_evidence=_record(result.get("threshold_evidence", {})),
        pilot_evidence=_record(result.get("pilot_evidence", {})),
        exclusions=[
            {
                key: str(_enum_value(_record(item).get(key, "")) or "")[
                    : (1000 if key == "message" else 256)
                ]
                for key in ("code", "severity", "message", "entity_kind", "source_id")
            }
            for item in (result.get("exclusions", ()) or ())[:50]
        ],
        exclusion_total=len(result.get("exclusions", ()) or ()),
        drift_report=[
            {
                key: str(_record(item).get(key, "") or "")
                for key in ("category", "course", "field", "approved", "current")
            }
            for item in (result.get("drift_report", ()) or ())
        ][:100],
    )
    error_code = str(result.get("error", "") or "")
    status = str(result.get("status", "") or "")
    if error_code:
        if error_code in {"OR-STUDENT-GATE-CLOSED", "OR-GATE-DRIFT"}:
            error_phase = "Student release gate"
        elif error_code in {"OR-RECOVERY-REQUIRED", "OR-EXECUTION-PAUSED"}:
            error_phase = "Batch recovery"
        elif error_code in {
            "OR-MANIFEST-DRIFT",
            "OR-SOURCE-DRIFT",
            "OR-CONFIG-DRIFT",
            "OR-SCOPE-DRIFT",
            "OR-THRESHOLD-HOLD",
            "OR-LIVE-NOT-STABLE",
        }:
            error_phase = "Safety revalidation"
        else:
            error_phase = "Manifest execution"
        mutation_status = (
            "Mutation may have happened; review the persisted action results before continuing."
            if status in {"partial", "paused", "recovery_required", "running"}
            or any(action.get("status") == "applied" for action in actions)
            else "No mutation is recorded for this stopped attempt."
        )
        if status == "stale":
            safe_next_action = "Keep this approval read-only and build a fresh plan from the retained import."
        elif status == "recovery_required":
            safe_next_action = "Reconcile the interrupted batch from live state before any retry."
        elif status == "paused":
            safe_next_action = "Review progress, then resume only after fresh safety revalidation."
        elif status == "awaiting_students":
            safe_next_action = "Arm and revalidate the student gate for this exact manifest."
        else:
            safe_next_action = "Review the action results and rebuild the plan before retrying changed work."
        result.update(
            error_phase=error_phase,
            mutation_status=mutation_status,
            safe_next_action=safe_next_action,
        )
    result.pop("live_evidence", None)
    return result


def _manifest_set_records(value: Any) -> list[dict[str, Any]]:
    result = _record(value)
    manifests: list[dict[str, Any]] = []
    for key in ("ordinary", "archive", "ownership"):
        manifest = result.get(key)
        if manifest is not None:
            manifests.append(_manifest_record(manifest))
    return manifests


async def _await(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


def _compatible_call(method: Any, candidates: Iterable[tuple[tuple, dict]]) -> Any:
    try:
        signature = inspect.signature(method)
    except (TypeError, ValueError):
        signature = None
    for args, kwargs in candidates:
        if signature is not None:
            try:
                signature.bind(*args, **kwargs)
            except TypeError:
                continue
        return method(*args, **kwargs)
    raise TypeError("The installed OneRoster service has an incompatible interface.")


async def _call(
    service: Any,
    names: Iterable[str],
    candidates: Iterable[tuple[tuple, dict]],
) -> Any:
    for name in names:
        method = getattr(service, name, None)
        if callable(method):
            if inspect.iscoroutinefunction(method):
                return await _await(_compatible_call(method, candidates))
            value = await asyncio.to_thread(_compatible_call, method, candidates)
            return await _await(value)
    raise TypeError("This OneRoster operation is not available in the installed profile.")


async def _feature(request: Request) -> dict[str, Any]:
    component = await component_context(request)
    service = getattr(request.app.state.gamgui, "oneroster_service", None)
    if not component["installed"]:
        return {
            "ready": False,
            "reason": "not_installed",
            "code": "CMP-NOT-INSTALLED",
            "message": "Install OneRoster Classroom from Settings → Components.",
            "component": component,
            "service": None,
        }
    if not component["enabled"]:
        return {
            "ready": False,
            "reason": "disabled",
            "code": "CMP-DISABLED",
            "message": "OneRoster Classroom is installed but disabled.",
            "component": component,
            "service": None,
        }
    if service is None:
        domain = str(
            getattr(request.app.state.gamgui, "audit_domain", "") or ""
        ).strip()
        if not domain:
            return {
                "ready": False,
                "reason": "auth_required",
                "code": "CMP-AUTH-REQUIRED",
                "message": (
                    "Connect a Workspace domain before opening OneRoster Import Studio."
                ),
                "component": component,
                "service": None,
            }
        return {
            "ready": False,
            "reason": "degraded",
            "code": "CMP-INCOMPATIBLE",
            "message": (
                "OneRoster Classroom could not start. Core Classroom tools remain available."
            ),
            "component": component,
            "service": None,
        }
    return {
        "ready": True,
        "reason": "",
        "code": "",
        "message": "",
        "component": component,
        "service": service,
    }


def _safe_error(exc: Exception, default_code: str = "OR-IMPORT-FAILED") -> tuple[str, str]:
    code = str(getattr(exc, "code", "") or default_code)
    if code == "OR-ACTIVE-JOB":
        code = "CMP-ACTIVE-JOB"
    message = str(getattr(exc, "public_message", "") or "")
    if not message:
        message = str(getattr(exc, "message", "") or "")
    if not message:
        message = (
            "OneRoster could not complete that local operation. "
            "No Classroom changes were made."
        )
    return code, message


def _execution_error(exc: Exception) -> tuple[str, str]:
    """Return a conservative message for a job that may have partially mutated Classroom."""

    code = str(getattr(exc, "code", "") or "OR-EXECUTION-FAILED")
    if code == "OR-ACTIVE-JOB":
        code = "CMP-ACTIVE-JOB"
    return (
        code,
        "Execution stopped. One or more Classroom changes may already have been "
        "applied. Review the persisted per-action results below, re-read live "
        "Classroom state, and create and confirm a new plan before continuing.",
    )


def _status(
    request: Request,
    *,
    notice: str = "",
    error: str = "",
    error_code: str = "",
) -> HTMLResponse:
    return TEMPLATES.TemplateResponse(
        request,
        "_oneroster_status.html",
        {"notice": notice, "error": error, "error_code": error_code},
    )


async def _ingest_uploaded_stream(
    service: Any,
    source: Any,
    filename: str,
) -> Any:
    return await _call(
        service,
        ("upload", "ingest_upload", "upload_snapshot", "ingest_zip"),
        (
            (
                (source,),
                {"filename": filename, "max_bytes": _MAX_UPLOAD_BYTES},
            ),
            ((source,), {"filename": filename}),
            (
                (),
                {
                    "upload": source,
                    "filename": filename,
                    "max_bytes": _MAX_UPLOAD_BYTES,
                },
            ),
        ),
    )


def _uploaded_snapshot_response(request: Request, value: Any) -> HTMLResponse:
    snapshot = _snapshot_record(value)
    import_id = str(snapshot.get("id", snapshot.get("import_id", "")) or "")
    if not _valid_import_id(import_id):
        return _status(
            request,
            error="The import service returned an invalid import identifier.",
            error_code="OR-IMPORT-FAILED",
        )
    snapshot.setdefault("id", import_id)
    return TEMPLATES.TemplateResponse(
        request,
        "_oneroster_guided_setup.html",
        {"snapshot": snapshot},
    )


def _valid_import_id(import_id: str) -> bool:
    return bool(_IMPORT_ID.fullmatch(import_id.strip()))


def _connector(request: Request) -> Any:
    return getattr(request.app.state.gamgui, "connector", None)


def _connector_required(request: Request) -> HTMLResponse:
    return _status(
        request,
        error=(
            "Connect and verify a Workspace domain before reading live Directory "
            "or Classroom state."
        ),
        error_code="CMP-AUTH-REQUIRED",
    )


def _task_maps(state: Any) -> tuple[dict[str, asyncio.Task], dict[str, tuple[str, str]]]:
    tasks = getattr(state, "oneroster_manifest_tasks", None)
    if not isinstance(tasks, dict):
        tasks = {}
        setattr(state, "oneroster_manifest_tasks", tasks)
    errors = getattr(state, "oneroster_manifest_errors", None)
    if not isinstance(errors, dict):
        errors = {}
        setattr(state, "oneroster_manifest_errors", errors)
    return tasks, errors


def _manifest_response(
    request: Request,
    manifest: Any,
    *,
    task_error: tuple[str, str] | None = None,
    notice: str = "",
    offset: int = 0,
    polling: bool = False,
    progress: Any = None,
    gate: Any = None,
) -> HTMLResponse:
    template = (
        "_oneroster_manifest.html"
        if request.headers.get("HX-Request", "").casefold() == "true"
        else "oneroster_activity.html"
    )
    return TEMPLATES.TemplateResponse(
        request,
        template,
        {
            "manifest": _manifest_record(manifest, offset=offset),
            "task_error_code": task_error[0] if task_error else "",
            "task_error": task_error[1] if task_error else "",
            "notice": notice,
            "polling": polling,
            "progress": _record(progress) if progress is not None else {},
            "gate": _record(gate) if gate is not None else {},
        },
    )


async def _manifest_progress(service: Any, manifest_id: str) -> Any:
    method = getattr(service, "get_execution_progress", None)
    if not callable(method):
        return None
    try:
        return await asyncio.to_thread(method, manifest_id)
    except (KeyError, TypeError):
        return None


async def _manifest_gate(service: Any) -> Any:
    return await _local_gate(service)


async def _run_manifest(
    service: Any,
    connector: Any,
    manifest_id: str,
    typed_import_id: str,
    errors: dict[str, tuple[str, str]],
) -> None:
    try:
        await _call(
            service,
            ("execute_manifest",),
            (
                (
                    (connector, manifest_id),
                    {"typed_import_id": typed_import_id},
                ),
                (
                    (),
                    {
                        "connector": connector,
                        "manifest_id": manifest_id,
                        "typed_import_id": typed_import_id,
                    },
                ),
            ),
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - exposed through stable engine codes
        errors[manifest_id] = _execution_error(exc)


def _start_manifest_task(
    state: Any,
    service: Any,
    connector: Any,
    manifest_id: str,
    typed_import_id: str,
) -> bool:
    tasks, errors = _task_maps(state)
    current = tasks.get(manifest_id)
    if current is not None and not current.done():
        return False
    errors.pop(manifest_id, None)
    task = asyncio.create_task(
        _run_manifest(
            service,
            connector,
            manifest_id,
            typed_import_id,
            errors,
        ),
        name=f"oneroster-manifest-{manifest_id}",
    )
    tasks[manifest_id] = task

    def discard_finished(done: asyncio.Task) -> None:
        if tasks.get(manifest_id) is done:
            tasks.pop(manifest_id, None)

    task.add_done_callback(discard_finished)
    return True


async def _build_plan(
    service: Any,
    connector: Any,
    import_id: str,
    *,
    limited_import: bool,
) -> Any:
    return await _call(
        service,
        ("build_live_plan",),
        (
            (
                (connector, import_id),
                {"limited_import": limited_import},
            ),
            (
                (),
                {
                    "connector": connector,
                    "import_id": import_id,
                    "limited_import": limited_import,
                },
            ),
        ),
    )


async def _plan_response(
    request: Request,
    planning: Any,
    *,
    manifests: Any = None,
    notice: str = "",
    error: str = "",
    error_code: str = "",
    pilot_evidence: str = "",
    denial: Any = None,
) -> HTMLResponse:
    planning_view = await asyncio.to_thread(_planning_record, planning)
    manifest_views = (
        await asyncio.to_thread(_manifest_set_records, manifests)
        if manifests is not None
        else []
    )
    return TEMPLATES.TemplateResponse(
        request,
        "_oneroster_plan.html",
        {
            "planning": planning_view,
            "manifests": manifest_views,
            "notice": notice,
            "error": error,
            "error_code": error_code,
            "pilot_evidence": str(pilot_evidence or "")[:1000],
            "denial": _record(denial),
        },
    )


async def _plan_denial(service: Any, planning: Any) -> Any:
    method = getattr(service, "get_denial", None)
    if not callable(method):
        return None
    record = _record(planning)
    return await _call(
        service,
        ("get_denial",),
        (
            (
                (
                    str(record.get("import_id", "") or ""),
                    record.get("threshold_evaluation"),
                ),
                {},
            ),
            (
                (),
                {
                    "import_id": str(record.get("import_id", "") or ""),
                    "evaluation": record.get("threshold_evaluation"),
                },
            ),
        ),
    )


async def _local_history(service: Any) -> list[dict[str, Any]]:
    try:
        value = await _call(
            service,
            ("history", "list_imports", "imports"),
            (
                ((), {"limit": 50}),
                ((50,), {}),
                ((), {}),
            ),
        )
    except TypeError:
        return []
    return [_snapshot_record(item) for item in _records(value)[:50]]


async def _local_gate(service: Any) -> dict[str, Any]:
    try:
        value = await _call(
            service,
            ("get_gate", "gate_status", "get_student_gate", "student_gate"),
            (((), {}),),
        )
    except TypeError:
        return {"state": "CLOSED", "timezone": "America/Chicago"}
    result = _record(value)
    result["state"] = _enum_value(result.get("state", "CLOSED"))
    result.setdefault("state", "CLOSED")
    result.setdefault("timezone", "America/Chicago")
    result.setdefault("student_action_count", 0)
    manifest_id = str(result.get("manifest_id", "") or "")
    loader = getattr(service, "get_manifest_page", None)
    if manifest_id and callable(loader):
        try:
            page = await asyncio.to_thread(loader, manifest_id, offset=0, limit=1)
            result["student_action_count"] = _manifest_record(page).get(
                "student_action_count", 0
            )
        except (KeyError, TypeError):
            pass
    return result


async def _local_thresholds(service: Any) -> dict[str, Any]:
    try:
        value = await _call(
            service,
            ("get_threshold_profile", "threshold_profile", "thresholds"),
            (((), {}),),
        )
    except TypeError:
        return {"configured": False, "mode": "normal", "rules": {}}
    result = _record(value)
    result.setdefault("configured", False)
    result.setdefault("mode", "normal")
    limits = result.get("limits", {})
    if isinstance(limits, dict):
        result["limits"] = {
            str(key): _record(item) if not isinstance(item, dict) else dict(item)
            for key, item in limits.items()
        }
    else:
        result["limits"] = {}
    blackouts = result.get("blackouts", ())
    result["blackouts"] = [
        _record(item) if not isinstance(item, dict) else dict(item)
        for item in blackouts
    ]
    return result


async def _local_scope_readiness(service: Any) -> dict[str, Any]:
    """Read cached local proof only; this helper must never touch Keychain or GAM."""

    try:
        value = await _call(
            service,
            ("scope_readiness",),
            (((), {}),),
        )
    except TypeError:
        return {"ready": False, "verified_at": 0.0, "expires_at": 0.0}
    result = _record(value)
    result["ready"] = bool(result.get("ready", False))
    result["verified_at"] = float(result.get("verified_at", 0.0) or 0.0)
    result["expires_at"] = float(result.get("expires_at", 0.0) or 0.0)
    return result


async def _import_record(service: Any, import_id: str) -> dict[str, Any]:
    value = await _call(
        service,
        ("get_import", "get_snapshot", "snapshot"),
        (
            ((import_id,), {}),
            ((), {"import_id": import_id}),
        ),
    )
    result = _snapshot_record(value)
    result.setdefault("id", import_id)
    return result


async def _load_manifest_page(
    service: Any,
    manifest_id: str,
    *,
    offset: int = 0,
) -> Any:
    """Use the bounded SQL page contract when the sealed component provides it."""

    if callable(getattr(service, "get_manifest_page", None)):
        return await _call(
            service,
            ("get_manifest_page",),
            (
                ((manifest_id,), {"offset": offset, "limit": 50}),
                (
                    (),
                    {
                        "manifest_id": manifest_id,
                        "offset": offset,
                        "limit": 50,
                    },
                ),
            ),
        )
    return await _call(
        service,
        ("get_manifest",),
        (
            ((manifest_id,), {}),
            ((), {"manifest_id": manifest_id}),
        ),
    )


@router.get("", response_class=HTMLResponse)
async def oneroster_page(request: Request) -> HTMLResponse:
    feature = await _feature(request)
    history: list[dict[str, Any]] = []
    gate: dict[str, Any] = {}
    thresholds: dict[str, Any] = {}
    access: dict[str, Any] = {}
    active_snapshot: dict[str, Any] = {}
    active_manifest: dict[str, Any] = {}
    active_progress: dict[str, Any] = {}
    if feature["ready"]:
        # These contracts are local SQLite/config reads. No connector is consulted.
        history = await _local_history(feature["service"])
        gate = await _local_gate(feature["service"])
        thresholds = await _local_thresholds(feature["service"])
        access = await _local_scope_readiness(feature["service"])
        active_snapshot = history[0] if history else {}
        latest_method = getattr(feature["service"], "latest_manifest_header", None)
        if active_snapshot and callable(latest_method):
            latest = await _call(
                feature["service"],
                ("latest_manifest_header",),
                (
                    ((str(active_snapshot.get("id", "")),), {}),
                    ((), {"import_id": str(active_snapshot.get("id", ""))}),
                ),
            )
            latest_record = _record(latest)
            manifest_id = str(latest_record.get("id", "") or "")
            if manifest_id:
                try:
                    page = await _load_manifest_page(
                        feature["service"], manifest_id
                    )
                    active_manifest = _manifest_record(page)
                    progress = await _manifest_progress(
                        feature["service"], manifest_id
                    )
                    active_progress = (
                        _record(progress) if progress is not None else {}
                    )
                except KeyError:
                    # The manifest can be pruned between the bounded header and
                    # page reads. Falling back to setup is safer than showing a
                    # stale action surface.
                    active_manifest = {}
    return TEMPLATES.TemplateResponse(
        request,
        "oneroster.html",
        {
            "feature": feature,
            "history": history,
            "gate": gate,
            "thresholds": thresholds,
            "access": access,
            "active_snapshot": active_snapshot,
            "active_manifest": active_manifest,
            "active_progress": active_progress,
        },
    )


@router.post("/access/verify", response_class=HTMLResponse)
async def verify_live_access(
    request: Request,
    managed_alias: Annotated[str, Form()] = "",
    admin: Annotated[str, Form()] = "",
) -> HTMLResponse:
    """Exercise the read capabilities the OneRoster planner actually needs."""

    feature = await _feature(request)
    if not feature["ready"]:
        return _status(
            request,
            error=feature["message"],
            error_code=feature["code"],
        )
    state = request.app.state.gamgui
    alias = managed_alias.strip()
    if (
        not alias.startswith("Section_")
        or len(alias) > 512
        or any(character in alias for character in "\r\n\x00")
    ):
        return TEMPLATES.TemplateResponse(
            request,
            "_oneroster_access.html",
            {
                "access": await _local_scope_readiness(feature["service"]),
                "error": "Enter one existing managed alias beginning with Section_.",
                "error_code": "CMP-AUTH-REQUIRED",
                "diagnostics": (),
            },
        )
    connector = _connector(request)
    if connector is None:
        return _connector_required(request)
    lease = try_acquire_admin_activity(state, "oneroster-scope-verify")
    if lease is None:
        return TEMPLATES.TemplateResponse(
            request,
            "_oneroster_access.html",
            {
                "access": await _local_scope_readiness(feature["service"]),
                "error": ADMIN_ACTIVITY_BUSY_MESSAGE,
                "error_code": "CMP-ACTIVE-JOB",
                "diagnostics": (),
            },
        )
    diagnostics: list[dict[str, str]] = []
    try:
        directory = getattr(connector, "list_oneroster_directory", None)
        courses = getattr(connector, "list_oneroster_managed_courses", None)
        rosters = getattr(connector, "list_course_participants_many", None)
        if not callable(directory) or not callable(courses) or not callable(rosters):
            raise RuntimeError("Required OneRoster connector reads are unavailable.")
        directory_rows = await directory()
        diagnostics.append(
            {
                "capability": "Directory export",
                "status": "passed",
                "detail": f"{len(directory_rows)} identities returned.",
            }
        )
        course_rows = tuple(await courses((alias,)))
        exact = []
        for course in course_rows:
            raw_aliases = getattr(course, "aliases", ()) or _record(course).get(
                "aliases", ()
            )
            if any(
                str(item).strip().casefold()
                in {alias.casefold(), f"d:{alias}".casefold()}
                for item in raw_aliases
            ):
                exact.append(course)
        if len(exact) != 1:
            raise RuntimeError("Exact managed-alias lookup did not return one course.")
        course_id = str(
            getattr(exact[0], "id", "") or _record(exact[0]).get("id", "")
        ).strip()
        if not course_id:
            raise RuntimeError("Exact managed-alias lookup omitted the course ID.")
        diagnostics.append(
            {
                "capability": "Exact alias lookup",
                "status": "passed",
                "detail": "One exact managed course returned.",
            }
        )
        roster = await rosters((course_id,), "all")
        covers = getattr(roster, "covers", None)
        if callable(covers) and not covers((course_id,)):
            raise RuntimeError("Roster export omitted the requested course.")
        diagnostics.append(
            {
                "capability": "Course roster read",
                "status": "passed",
                "detail": f"{len(roster)} memberships returned with course coverage.",
            }
        )
        access_value = await _call(
            feature["service"],
            ("mark_scope_ready",),
            (((), {}),),
        )
        access = _record(access_value)
        notice = "OneRoster read capabilities passed for this connection."
        error = ""
        error_code = ""
    except Exception as exc:  # noqa: BLE001 - converted to stable operator error
        diagnostics.append(
            {
                "capability": "Diagnostic stopped",
                "status": "failed",
                "detail": "A required read did not return complete evidence.",
            }
        )
        invalidator = getattr(feature["service"], "invalidate_scope_readiness", None)
        if callable(invalidator):
            await asyncio.to_thread(invalidator)
        access = await _local_scope_readiness(feature["service"])
        active_snapshot = history[0] if history else {}
        code, message = _safe_error(exc, "CMP-AUTH-REQUIRED")
        notice = ""
        error = message
        error_code = code
    finally:
        lease.release()
    return TEMPLATES.TemplateResponse(
        request,
        "_oneroster_access.html",
        {
            "access": access,
            "active_snapshot": active_snapshot,
            "notice": notice,
            "error": error,
            "error_code": error_code,
            "missing_scopes": (),
            "diagnostics": diagnostics,
        },
    )


@router.post("/upload", response_class=HTMLResponse)
async def upload_snapshot(
    request: Request,
    package: Annotated[UploadFile, File()],
    replace_active: Annotated[str, Form()] = "",
) -> HTMLResponse:
    feature = await _feature(request)
    if not feature["ready"]:
        return _status(
            request,
            error=feature["message"],
            error_code=feature["code"],
        )
    filename = (package.filename or "").strip()
    if not filename.casefold().endswith(".zip"):
        return _status(
            request,
            error="Choose a OneRoster ZIP package.",
            error_code="OR-ZIP-INVALID",
        )
    size = getattr(package, "size", None)
    if isinstance(size, int) and size > _MAX_UPLOAD_BYTES:
        return _status(
            request,
            error="The package exceeds the 250 MB upload limit.",
            error_code="OR-ZIP-INVALID",
        )
    state = request.app.state.gamgui
    lease = try_acquire_admin_activity(state, "oneroster-snapshot-upload")
    if lease is None:
        return _status(
            request,
            error=ADMIN_ACTIVITY_BUSY_MESSAGE,
            error_code="CMP-ACTIVE-JOB",
        )
    bounded_source = _BoundedUploadStream(package.file, _MAX_UPLOAD_BYTES)
    try:
        if await _local_history(feature["service"]) and replace_active != "yes":
            return _status(
                request,
                error=(
                    "A guided import is already saved. Open 'Replace the current "
                    "roster' and confirm the replacement before choosing a new file."
                ),
                error_code="OR-ACTIVE-JOURNEY",
            )
        value = await _ingest_uploaded_stream(
            feature["service"],
            bounded_source,
            filename,
        )
    except Exception as exc:  # noqa: BLE001 - stable local error contract
        code, message = _safe_error(exc, "OR-ZIP-INVALID")
        return _status(request, error=message, error_code=code)
    finally:
        lease.release()
    return _uploaded_snapshot_response(request, value)


@router.post("/upload-folder", response_class=HTMLResponse)
async def upload_snapshot_folder(
    request: Request,
    folder_files: Annotated[list[UploadFile], File()],
    replace_active: Annotated[str, Form()] = "",
) -> HTMLResponse:
    feature = await _feature(request)
    if not feature["ready"]:
        return _status(
            request,
            error=feature["message"],
            error_code=feature["code"],
        )
    state = request.app.state.gamgui
    lease = try_acquire_admin_activity(state, "oneroster-folder-upload")
    if lease is None:
        return _status(
            request,
            error=ADMIN_ACTIVITY_BUSY_MESSAGE,
            error_code="CMP-ACTIVE-JOB",
        )
    archive = None
    try:
        archive, filename = await asyncio.to_thread(
            _folder_upload_archive,
            folder_files,
        )
        if await _local_history(feature["service"]) and replace_active != "yes":
            return _status(
                request,
                error=(
                    "A guided import is already saved. Open 'Replace the current "
                    "roster' and confirm the replacement before choosing a new folder."
                ),
                error_code="OR-ACTIVE-JOURNEY",
            )
        value = await _ingest_uploaded_stream(
            feature["service"],
            archive,
            filename,
        )
    except Exception as exc:  # noqa: BLE001 - stable local error contract
        code, message = _safe_error(exc, "OR-FOLDER-INVALID")
        return _status(request, error=message, error_code=code)
    finally:
        if archive is not None:
            archive.close()
        lease.release()
    return _uploaded_snapshot_response(request, value)


@router.get("/import/{import_id}", response_class=HTMLResponse)
async def import_detail(request: Request, import_id: str) -> HTMLResponse:
    feature = await _feature(request)
    if not feature["ready"]:
        return _status(
            request,
            error=feature["message"],
            error_code=feature["code"],
        )
    if not _valid_import_id(import_id):
        return _status(
            request,
            error="That import identifier is invalid.",
            error_code="OR-IMPORT-NOT-FOUND",
        )
    try:
        snapshot = await _import_record(feature["service"], import_id)
    except Exception as exc:  # noqa: BLE001
        code, message = _safe_error(exc, "OR-IMPORT-NOT-FOUND")
        return _status(request, error=message, error_code=code)
    return TEMPLATES.TemplateResponse(
        request,
        "_oneroster_guided_setup.html",
        {"snapshot": snapshot},
    )


@router.post("/import/{import_id}/session", response_class=HTMLResponse)
async def select_academic_session(
    request: Request,
    import_id: str,
    session_id: Annotated[str, Form()],
) -> HTMLResponse:
    feature = await _feature(request)
    if not feature["ready"]:
        return _status(
            request,
            error=feature["message"],
            error_code=feature["code"],
        )
    normalized_session = session_id.strip()
    if (
        not _valid_import_id(import_id)
        or not normalized_session
        or len(normalized_session) > 128
        or any(ord(character) < 32 for character in normalized_session)
    ):
        return _status(
            request,
            error="Choose a valid academic session from this snapshot.",
            error_code="OR-SESSION-INVALID",
        )
    state = request.app.state.gamgui
    lease = try_acquire_admin_activity(state, "oneroster-session-select")
    if lease is None:
        return _status(
            request,
            error=ADMIN_ACTIVITY_BUSY_MESSAGE,
            error_code="CMP-ACTIVE-JOB",
        )
    try:
        value = await _call(
            feature["service"],
            ("select_session",),
            (
                ((import_id, normalized_session), {}),
                (
                    (),
                    {
                        "import_id": import_id,
                        "session_id": normalized_session,
                    },
                ),
            ),
        )
    except Exception as exc:  # noqa: BLE001
        code, message = _safe_error(exc, "OR-SESSION-INVALID")
        return _status(request, error=message, error_code=code)
    finally:
        lease.release()
    snapshot = _snapshot_record(value)
    snapshot.setdefault("id", import_id)
    return TEMPLATES.TemplateResponse(
        request,
        "_oneroster_guided_setup.html",
        {
            "snapshot": snapshot,
            "notice": (
                "Automatic class and enrollment date scope refreshed locally. "
                "No Classroom changes were made."
            ),
        },
    )


@router.post("/import/{import_id}/naming", response_class=HTMLResponse)
async def configure_course_naming(
    request: Request,
    import_id: str,
    naming_choice: Annotated[str, Form()] = "default",
    custom_template: Annotated[str, Form()] = "",
) -> HTMLResponse:
    feature = await _feature(request)
    if not feature["ready"]:
        return _status(
            request,
            error=feature["message"],
            error_code=feature["code"],
        )
    if not _valid_import_id(import_id):
        return _status(
            request,
            error="That import identifier is invalid.",
            error_code="OR-IMPORT-NOT-FOUND",
        )
    choice = naming_choice.strip().casefold()
    if choice == "custom":
        template = custom_template.strip()
        if not template:
            return _status(
                request,
                error="Enter a custom class naming template.",
                error_code="OR-NAMING-INVALID",
            )
    elif choice in _COURSE_NAME_PRESETS:
        template = _COURSE_NAME_PRESETS[choice]
    else:
        return _status(
            request,
            error="Choose one of the available class naming schemes.",
            error_code="OR-NAMING-INVALID",
        )
    state = request.app.state.gamgui
    lease = try_acquire_admin_activity(state, "oneroster-course-naming")
    if lease is None:
        return _status(
            request,
            error=ADMIN_ACTIVITY_BUSY_MESSAGE,
            error_code="CMP-ACTIVE-JOB",
        )
    try:
        value = await _call(
            feature["service"],
            ("configure_course_naming", "configure_naming"),
            (
                ((import_id, template), {}),
                (
                    (),
                    {
                        "import_id": import_id,
                        "template": template,
                    },
                ),
            ),
        )
    except Exception as exc:  # noqa: BLE001
        code, message = _safe_error(exc, "OR-NAMING-INVALID")
        return _status(request, error=message, error_code=code)
    finally:
        lease.release()
    snapshot = _snapshot_record(value)
    snapshot.setdefault("id", import_id)
    return TEMPLATES.TemplateResponse(
        request,
        "_oneroster_guided_setup.html",
        {
            "snapshot": snapshot,
            "notice": (
                "Class names were rebuilt locally with the selected scheme. "
                "Review the Courses preview before building a live plan."
            ),
        },
    )


@router.get("/import/{import_id}/export/{kind}")
async def export_gam_csv(
    request: Request,
    import_id: str,
    kind: str,
) -> Response:
    feature = await _feature(request)
    if not feature["ready"]:
        return _status(
            request,
            error=feature["message"],
            error_code=feature["code"],
        )
    export_kind = kind.strip().casefold().removesuffix(".csv")
    if not _valid_import_id(import_id) or export_kind not in _EXPORT_KINDS:
        return _status(
            request,
            error="Choose a supported GAM-ready OneRoster export.",
            error_code="OR-EXPORT-INVALID",
        )

    stream = tempfile.TemporaryFile(mode="w+b")
    try:
        count = await _call(
            feature["service"],
            ("write_export",),
            (
                ((import_id, export_kind, stream), {}),
                (
                    (),
                    {
                        "import_id": import_id,
                        "kind": export_kind,
                        "stream": stream,
                    },
                ),
            ),
        )
        row_count = max(0, int(count or 0))
        stream.seek(0)
    except Exception as exc:  # noqa: BLE001
        stream.close()
        code, message = _safe_error(exc, "OR-EXPORT-FAILED")
        return _status(request, error=message, error_code=code)

    async def chunks() -> AsyncIterator[bytes]:
        try:
            while True:
                chunk = await asyncio.to_thread(stream.read, 64 * 1024)
                if not chunk:
                    break
                yield chunk
        finally:
            stream.close()

    safe_name = f"oneroster-{import_id}-{export_kind}.csv"
    return StreamingResponse(
        chunks(),
        media_type="text/csv; charset=utf-8",
        headers={
            "Cache-Control": "no-store",
            "Content-Disposition": f'attachment; filename="{safe_name}"',
            "X-Content-Type-Options": "nosniff",
            "X-OneRoster-Row-Count": str(row_count),
        },
    )


@router.post("/import/{import_id}/validate", response_class=HTMLResponse)
async def validate_snapshot(request: Request, import_id: str) -> HTMLResponse:
    feature = await _feature(request)
    if not feature["ready"]:
        return _status(
            request,
            error=feature["message"],
            error_code=feature["code"],
        )
    if not _valid_import_id(import_id):
        return _status(
            request,
            error="That import identifier is invalid.",
            error_code="OR-IMPORT-NOT-FOUND",
        )
    state = request.app.state.gamgui
    lease = try_acquire_admin_activity(state, "oneroster-snapshot-validate")
    if lease is None:
        return _status(
            request,
            error=ADMIN_ACTIVITY_BUSY_MESSAGE,
            error_code="CMP-ACTIVE-JOB",
        )
    try:
        try:
            value = await _call(
                feature["service"],
                ("validate", "validate_import", "validate_snapshot"),
                (
                    ((import_id,), {}),
                    ((), {"import_id": import_id}),
                ),
            )
        except TypeError:
            # The first-party engine validates atomically during upload. A subsequent
            # local read is the explicit verification boundary for this UI action.
            value = await _import_record(feature["service"], import_id)
    except Exception as exc:  # noqa: BLE001
        code, message = _safe_error(exc, "OR-MANIFEST-MISMATCH")
        return _status(request, error=message, error_code=code)
    finally:
        lease.release()
    snapshot = _snapshot_record(value)
    snapshot.setdefault("id", import_id)
    return TEMPLATES.TemplateResponse(
        request,
        "_oneroster_guided_setup.html",
        {
            "snapshot": snapshot,
            "notice": "Local validation completed. No Classroom changes were made.",
        },
    )


@router.get("/import/{import_id}/preview", response_class=HTMLResponse)
async def preview_snapshot(
    request: Request,
    import_id: str,
    kind: str = "courses",
    q: str = "",
    cursor: str = "",
    limit: int = 50,
) -> HTMLResponse:
    feature = await _feature(request)
    if not feature["ready"]:
        return _status(
            request,
            error=feature["message"],
            error_code=feature["code"],
        )
    if not _valid_import_id(import_id):
        return _status(
            request,
            error="That import identifier is invalid.",
            error_code="OR-IMPORT-NOT-FOUND",
        )
    kind = kind.strip().casefold()
    if kind not in _PREVIEW_KINDS:
        return _status(
            request,
            error="Choose a supported OneRoster preview.",
            error_code="OR-PREVIEW-INVALID",
        )
    query = q.strip()[:200]
    page_size = min(50, max(1, limit))
    try:
        value = await _call(
            feature["service"],
            ("preview", "preview_import", "list_preview"),
            (
                (
                    (import_id,),
                    {
                        "kind": kind,
                        "query": query,
                        "cursor": cursor or None,
                        "limit": page_size,
                    },
                ),
                (
                    (),
                    {
                        "import_id": import_id,
                        "kind": kind,
                        "query": query,
                        "cursor": cursor or None,
                        "limit": page_size,
                    },
                ),
            ),
        )
    except Exception as exc:  # noqa: BLE001
        code, message = _safe_error(exc, "OR-PREVIEW-FAILED")
        return _status(request, error=message, error_code=code)
    page = _record(value)
    rows = _records(page.get("items", page.get("rows", [])))[:page_size]
    if kind == "courses" and rows:
        snapshot = await _import_record(feature["service"], import_id)
        initial_scope = str(snapshot.get("initial_scope_date", "") or "")
        for row in rows:
            evaluated = str(row.get("scope_date", "") or "")
            starts = str(row.get("term_start", "") or "")
            try:
                evaluated_date = date.fromisoformat(evaluated)
                start_date = date.fromisoformat(starts)
                row["days_until_start"] = (start_date - evaluated_date).days
                row["creation_cutoff"] = (
                    evaluated_date + timedelta(days=31)
                ).isoformat()
                if initial_scope:
                    initial_date = date.fromisoformat(initial_scope)
                    was_eligible = start_date <= initial_date + timedelta(days=31)
                    is_eligible = start_date <= evaluated_date + timedelta(days=31)
                    row["changed_since_upload"] = (
                        "Became eligible"
                        if not was_eligible and is_eligible
                        else "No"
                    )
            except ValueError:
                row.setdefault("days_until_start", "")
                row.setdefault("creation_cutoff", "")
                row.setdefault("changed_since_upload", "")
    columns = page.get("columns")
    if not isinstance(columns, (list, tuple)):
        available = list(rows[0].keys()) if rows else []
        preferred = [
            column
            for column in _PREFERRED_PREVIEW_COLUMNS.get(kind, ())
            if column in available
        ]
        columns = [
            *preferred,
            *(column for column in available if column not in preferred),
        ][:10]
    columns = [str(column) for column in columns][:10]
    context = {
            "import_id": import_id,
            "kind": kind,
            "query": query,
            "columns": columns,
            "total": int(page.get("total", len(rows)) or 0),
            "total_exact": bool(page.get("total_exact", True)),
            "next_cursor": str(page.get("next_cursor", "") or ""),
            "limit": page_size,
        }
    response: HTMLResponse | None = None
    for maximum_chars in (512, 256, 128, 64, 32, 16, 8):
        context["rows"] = _bounded_preview_rows(
            rows,
            columns,
            maximum_chars,
        )
        response = TEMPLATES.TemplateResponse(
            request,
            "_oneroster_preview.html",
            context,
        )
        if len(response.body) <= 100_000:
            return response
    assert response is not None
    return response


@router.post("/import/{import_id}/plan", response_class=HTMLResponse)
async def build_live_plan(
    request: Request,
    import_id: str,
    mode: Annotated[str, Form()] = "normal",
    pilot_evidence: Annotated[str, Form()] = "",
) -> HTMLResponse:
    feature = await _feature(request)
    if not feature["ready"]:
        return _status(
            request,
            error=feature["message"],
            error_code=feature["code"],
        )
    if not _valid_import_id(import_id):
        return _status(
            request,
            error="That import identifier is invalid.",
            error_code="OR-IMPORT-NOT-FOUND",
        )
    normalized_mode = mode.strip().casefold()
    if normalized_mode not in {"normal", "limited"}:
        return _status(
            request,
            error="Choose a full or additions-only live plan.",
            error_code="OR-PLAN-INVALID",
        )
    connector = _connector(request)
    if connector is None:
        return _connector_required(request)
    pilot_note = pilot_evidence.strip()
    if len(pilot_note) < 8 or len(pilot_note) > 1000:
        return _status(
            request,
            error=(
                "Enter pilot or rollout evidence between 8 and 1,000 characters "
                "before creating an immutable district manifest."
            ),
            error_code="OR-PILOT-EVIDENCE",
        )

    state = request.app.state.gamgui
    lease = try_acquire_admin_activity(state, "oneroster-live-plan")
    if lease is None:
        return _status(
            request,
            error=ADMIN_ACTIVITY_BUSY_MESSAGE,
            error_code="CMP-ACTIVE-JOB",
        )
    try:
        planning = await _build_plan(
            feature["service"],
            connector,
            import_id,
            limited_import=normalized_mode == "limited",
        )
        planning_data = _record(planning)
        effective_limited = bool(planning_data.get("limited_import", False))
        evaluation = _evaluation_record(
            planning_data.get("threshold_evaluation", {})
        )
        if evaluation["held"] and not effective_limited:
            return await _plan_response(
                request,
                planning,
                pilot_evidence=pilot_note,
                denial=await _plan_denial(feature["service"], planning),
            )
        manifests = await _call(
            feature["service"],
            ("persist_live_plan",),
            (
                ((planning,), {"pilot_evidence": pilot_note}),
                (
                    (),
                    {
                        "planning": planning,
                        "pilot_evidence": pilot_note,
                    },
                ),
                ((planning,), {}),
            ),
        )
    except Exception as exc:  # noqa: BLE001
        code, message = _safe_error(exc, "OR-PLAN-FAILED")
        return _status(request, error=message, error_code=code)
    finally:
        lease.release()
    return await _plan_response(
        request,
        planning,
        manifests=manifests,
        notice=(
            "Immutable additions-only manifests created."
            if effective_limited
            else "Immutable full-plan manifests created."
        ),
        pilot_evidence=pilot_note,
        denial=await _plan_denial(feature["service"], planning),
    )


@router.post("/import/{import_id}/override", response_class=HTMLResponse)
async def override_threshold_hold(
    request: Request,
    import_id: str,
    typed_import_id: Annotated[str, Form()],
    reason: Annotated[str, Form()],
    expected_plan_hash: Annotated[str, Form()],
    pilot_evidence: Annotated[str, Form()] = "",
) -> HTMLResponse:
    feature = await _feature(request)
    if not feature["ready"]:
        return _status(
            request,
            error=feature["message"],
            error_code=feature["code"],
        )
    if not _valid_import_id(import_id):
        return _status(
            request,
            error="That import identifier is invalid.",
            error_code="OR-IMPORT-NOT-FOUND",
        )
    if not re.fullmatch(r"[0-9a-f]{64}", expected_plan_hash.strip().casefold()):
        return _status(
            request,
            error="The held-plan evidence is missing or invalid. Build the live plan again.",
            error_code="OR-PLAN-DRIFT",
        )
    normalized_reason = reason.strip()
    if len(normalized_reason) < 8 or len(normalized_reason) > 500:
        return _status(
            request,
            error="Enter an audit reason between 8 and 500 characters.",
            error_code="OR-OVERRIDE-REASON",
        )
    pilot_note = pilot_evidence.strip()
    if len(pilot_note) < 8 or len(pilot_note) > 1000:
        return _status(
            request,
            error="Enter pilot or rollout evidence between 8 and 1,000 characters.",
            error_code="OR-PILOT-EVIDENCE",
        )
    connector = _connector(request)
    if connector is None:
        return _connector_required(request)

    state = request.app.state.gamgui
    lease = try_acquire_admin_activity(state, "oneroster-threshold-override")
    if lease is None:
        return _status(
            request,
            error=ADMIN_ACTIVITY_BUSY_MESSAGE,
            error_code="CMP-ACTIVE-JOB",
        )
    try:
        planning = await _build_plan(
            feature["service"],
            connector,
            import_id,
            limited_import=False,
        )
        if (
            await _planning_confirmation_hash_async(planning)
            != expected_plan_hash.strip().casefold()
        ):
            return await _plan_response(
                request,
                planning,
                error=(
                    "Live Directory, Classroom, source, or threshold evidence changed. "
                    "Review this new held plan before overriding it."
                ),
                error_code="OR-PLAN-DRIFT",
                pilot_evidence=pilot_note,
            )
        evaluation = _record(planning).get("threshold_evaluation")
        await _call(
            feature["service"],
            ("record_override",),
            (
                (
                    (
                        import_id,
                        evaluation,
                        normalized_reason,
                        typed_import_id,
                    ),
                    {},
                ),
                (
                    (),
                    {
                        "import_id": import_id,
                        "evaluation": evaluation,
                        "reason": normalized_reason,
                        "typed_import_id": typed_import_id,
                    },
                ),
            ),
        )
        manifests = await _call(
            feature["service"],
            ("persist_live_plan",),
            (
                ((planning,), {"pilot_evidence": pilot_note}),
                (
                    (),
                    {
                        "planning": planning,
                        "pilot_evidence": pilot_note,
                    },
                ),
                ((planning,), {}),
            ),
        )
    except Exception as exc:  # noqa: BLE001
        code, message = _safe_error(exc, "OR-OVERRIDE-FAILED")
        return _status(request, error=message, error_code=code)
    finally:
        lease.release()
    return await _plan_response(
        request,
        planning,
        manifests=manifests,
        notice=(
            "Threshold override recorded with immutable evidence. "
            "Each resulting manifest still requires typed confirmation."
        ),
        pilot_evidence=pilot_note,
        denial=await _plan_denial(feature["service"], planning),
    )


@router.post("/import/{import_id}/deny", response_class=HTMLResponse)
async def deny_threshold_hold(
    request: Request,
    import_id: str,
    typed_import_id: Annotated[str, Form()],
    reason: Annotated[str, Form()],
    expected_plan_hash: Annotated[str, Form()],
) -> HTMLResponse:
    feature = await _feature(request)
    if not feature["ready"]:
        return _status(
            request,
            error=feature["message"],
            error_code=feature["code"],
        )
    if not _valid_import_id(import_id) or not _MANIFEST_HASH.fullmatch(
        expected_plan_hash.strip().casefold()
    ):
        return _status(
            request,
            error="The held-plan evidence is invalid. Build the live plan again.",
            error_code="OR-PLAN-DRIFT",
        )
    normalized_reason = reason.strip()
    if len(normalized_reason) < 8 or len(normalized_reason) > 500:
        return _status(
            request,
            error="Enter a denial reason between 8 and 500 characters.",
            error_code="OR-DENY-REASON",
        )
    connector = _connector(request)
    if connector is None:
        return _connector_required(request)
    state = request.app.state.gamgui
    lease = try_acquire_admin_activity(state, "oneroster-threshold-deny")
    if lease is None:
        return _status(
            request,
            error=ADMIN_ACTIVITY_BUSY_MESSAGE,
            error_code="CMP-ACTIVE-JOB",
        )
    try:
        planning = await _build_plan(
            feature["service"],
            connector,
            import_id,
            limited_import=False,
        )
        if (
            await _planning_confirmation_hash_async(planning)
            != expected_plan_hash.strip().casefold()
        ):
            return await _plan_response(
                request,
                planning,
                error=(
                    "Live Directory, Classroom, source, or threshold evidence changed. "
                    "Review this new held plan before denying it."
                ),
                error_code="OR-PLAN-DRIFT",
            )
        evaluation = _record(planning).get("threshold_evaluation")
        await _call(
            feature["service"],
            ("record_denial",),
            (
                (
                    (
                        import_id,
                        evaluation,
                        normalized_reason,
                        typed_import_id,
                    ),
                    {},
                ),
                (
                    (),
                    {
                        "import_id": import_id,
                        "evaluation": evaluation,
                        "reason": normalized_reason,
                        "typed_import_id": typed_import_id,
                    },
                ),
            ),
        )
    except Exception as exc:  # noqa: BLE001
        code, message = _safe_error(exc, "OR-DENY-FAILED")
        return _status(request, error=message, error_code=code)
    finally:
        lease.release()
    return await _plan_response(
        request,
        planning,
        notice=(
            "This exact held plan was denied and recorded. No manifest was "
            "created and no Classroom change was made."
        ),
        denial=await _plan_denial(feature["service"], planning),
    )


@router.get("/thresholds", response_class=HTMLResponse)
async def threshold_settings(request: Request) -> HTMLResponse:
    feature = await _feature(request)
    if not feature["ready"]:
        return _status(
            request,
            error=feature["message"],
            error_code=feature["code"],
        )
    return TEMPLATES.TemplateResponse(
        request,
        "_oneroster_thresholds.html",
        {"thresholds": await _local_thresholds(feature["service"])},
    )


def _number(raw: Any, *, percent: bool) -> int | float | None:
    text = str(raw or "").strip()
    if not text:
        return None
    value = float(text) if percent else int(text)
    maximum = 100 if percent else 1_000_000_000
    if value < 0 or value > maximum:
        raise ValueError
    return value


def _chicago_aware(value: str) -> datetime:
    """Attach the post-2007 America/Chicago offset without a tzdata dependency.

    macOS supplies IANA timezone data, but minimal Windows CI images may not.  The
    ambiguous/nonexistent DST transition hours fail closed instead of guessing.
    """

    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is not None:
        return parsed

    march_first = datetime(parsed.year, 3, 1)
    first_march_sunday = 1 + ((6 - march_first.weekday()) % 7)
    second_march_sunday = first_march_sunday + 7
    november_first = datetime(parsed.year, 11, 1)
    first_november_sunday = 1 + ((6 - november_first.weekday()) % 7)

    spring_date = (3, second_march_sunday)
    fall_date = (11, first_november_sunday)
    local_date = (parsed.month, parsed.day)
    if local_date == spring_date and parsed.hour == 2:
        raise ValueError("That local time does not exist during the DST transition.")
    if local_date == fall_date and parsed.hour == 1:
        raise ValueError("That local time is ambiguous during the DST transition.")

    dst_start = datetime(parsed.year, 3, second_march_sunday, 3)
    dst_end = datetime(parsed.year, 11, first_november_sunday, 1)
    offset = -5 if dst_start <= parsed < dst_end else -6
    return parsed.replace(tzinfo=timezone(timedelta(hours=offset)))


def _parse_additional_blackouts(
    raw: str,
    *,
    remaining: int,
) -> list[dict[str, str]]:
    lines = [line.strip() for line in str(raw or "").splitlines() if line.strip()]
    if len(lines) > remaining:
        raise ValueError("At most 10 blackout periods may be configured.")
    result: list[dict[str, str]] = []
    for line in lines:
        parts = [part.strip() for part in line.split("|", 2)]
        if len(parts) not in {2, 3} or not parts[0] or not parts[1]:
            raise ValueError(
                "Each additional blackout must use start | end | optional label."
            )
        start = _chicago_aware(parts[0])
        end = _chicago_aware(parts[1])
        if end <= start:
            raise ValueError("A blackout end must be after its start.")
        label = parts[2] if len(parts) == 3 else "District blackout"
        if len(label) > 80:
            raise ValueError("Blackout labels must be 80 characters or fewer.")
        result.append(
            {
                "starts_at": start.isoformat(),
                "ends_at": end.isoformat(),
                "label": label or "District blackout",
            }
        )
    return result


@router.post("/thresholds", response_class=HTMLResponse)
async def save_threshold_settings(request: Request) -> HTMLResponse:
    feature = await _feature(request)
    if not feature["ready"]:
        return _status(
            request,
            error=feature["message"],
            error_code=feature["code"],
        )
    state = request.app.state.gamgui
    # Prefer the lifecycle refusal over form diagnostics when another operation
    # already owns the exclusive registry. The authoritative lease is acquired
    # again immediately before persistence, so a race still fails closed.
    activity_probe = try_acquire_admin_activity(
        state,
        "oneroster-threshold-validation",
    )
    if activity_probe is None:
        return _status(
            request,
            error=ADMIN_ACTIVITY_BUSY_MESSAGE,
            error_code="CMP-ACTIVE-JOB",
        )
    activity_probe.release()
    form = await request.form()
    mode = str(form.get("mode", "normal")).strip().casefold()
    if mode not in {"normal", "limited"}:
        return _status(
            request,
            error="Choose Normal or Limited Import mode.",
            error_code="OR-THRESHOLD-INVALID",
        )
    try:
        rules = {
            key: _number(form.get(key), percent=key.endswith("_percent"))
            for key in sorted(_THRESHOLD_KEYS)
        }
    except (TypeError, ValueError):
        return _status(
            request,
            error="Threshold counts must be non-negative whole numbers and percentages must be between 0 and 100.",
            error_code="OR-THRESHOLD-INVALID",
        )
    deliberately_disabled = {
        action: str(form.get(f"{action}_disabled", "") or "").strip().casefold()
        in _CHECKED_VALUES
        for action in _THRESHOLD_ACTIONS
    }
    for action in _THRESHOLD_ACTIONS:
        max_count = rules[f"{action}_count"]
        max_percent = rules[f"{action}_percent"]
        disabled = deliberately_disabled[action]
        if max_count is None and max_percent is None and not disabled:
            return _status(
                request,
                error=(
                    "Every action needs a count or percentage threshold, or an "
                    "explicit No limit selection."
                ),
                error_code="OR-THRESHOLD-INCOMPLETE",
            )
        if disabled and (max_count is not None or max_percent is not None):
            return _status(
                request,
                error=(
                    "An action cannot have a threshold and No limit selected at "
                    "the same time."
                ),
                error_code="OR-THRESHOLD-INVALID",
            )
    blackout_start = str(form.get("blackout_start", "")).strip()
    blackout_end = str(form.get("blackout_end", "")).strip()
    additional_blackouts = str(form.get("additional_blackouts", "") or "")
    if bool(blackout_start) != bool(blackout_end):
        return _status(
            request,
            error="Enter both the blackout start and end, or leave both blank.",
            error_code="OR-THRESHOLD-INVALID",
        )
    blackouts: list[dict[str, str]] = []
    if blackout_start and blackout_end:
        try:
            start = _chicago_aware(blackout_start)
            end = _chicago_aware(blackout_end)
            if end <= start:
                raise ValueError
        except ValueError:
            return _status(
                request,
                error="The blackout end must be after its start.",
                error_code="OR-THRESHOLD-INVALID",
            )
        blackouts.append(
            {
                "starts_at": start.isoformat(),
                "ends_at": end.isoformat(),
                "label": "District blackout",
            }
        )
    try:
        blackouts.extend(
            _parse_additional_blackouts(
                additional_blackouts,
                remaining=10 - len(blackouts),
            )
        )
    except ValueError as exc:
        return _status(
            request,
            error=str(exc),
            error_code="OR-THRESHOLD-INVALID",
        )
    limits = {
        action: {
            "max_count": rules[f"{action}_count"],
            "max_percent": rules[f"{action}_percent"],
        }
        for action in _THRESHOLD_ACTIONS
    }
    profile = {
        "version": 1,
        "configured": True,
        "limited_import": mode == "limited",
        "limits": limits,
        "blackouts": blackouts,
    }
    lease = try_acquire_admin_activity(state, "oneroster-threshold-save")
    if lease is None:
        return _status(
            request,
            error=ADMIN_ACTIVITY_BUSY_MESSAGE,
            error_code="CMP-ACTIVE-JOB",
        )
    try:
        value = await _call(
            feature["service"],
            ("save_threshold_profile", "set_threshold_profile", "save_thresholds"),
            (
                ((profile,), {}),
                ((), {"profile": profile}),
            ),
        )
    except Exception as exc:  # noqa: BLE001
        code, message = _safe_error(exc, "OR-THRESHOLD-INVALID")
        return _status(request, error=message, error_code=code)
    finally:
        lease.release()
    thresholds = _record(value) or profile
    thresholds.setdefault("configured", True)
    return TEMPLATES.TemplateResponse(
        request,
        "_oneroster_thresholds.html",
        {
            "thresholds": thresholds,
            "notice": "Threshold profile saved.",
        },
    )


@router.get("/manifest/{manifest_id}", response_class=HTMLResponse)
async def manifest_detail(
    request: Request,
    manifest_id: str,
    offset: int = 0,
) -> HTMLResponse:
    feature = await _feature(request)
    if not feature["ready"]:
        return _status(
            request,
            error=feature["message"],
            error_code=feature["code"],
        )
    if not _valid_import_id(manifest_id):
        return _status(
            request,
            error="That manifest identifier is invalid.",
            error_code="OR-MANIFEST-NOT-FOUND",
        )
    try:
        manifest = await _load_manifest_page(
            feature["service"], manifest_id, offset=offset
        )
    except Exception as exc:  # noqa: BLE001
        code, message = _safe_error(exc, "OR-MANIFEST-NOT-FOUND")
        return _status(request, error=message, error_code=code)
    state = request.app.state.gamgui
    tasks, errors = _task_maps(state)
    task = tasks.get(manifest_id)
    return _manifest_response(
        request,
        manifest,
        task_error=errors.get(manifest_id),
        offset=offset,
        polling=bool(task is not None and not task.done()),
        progress=await _manifest_progress(feature["service"], manifest_id),
        gate=await _manifest_gate(feature["service"]),
    )


@router.post("/manifest/{manifest_id}/execute", response_class=HTMLResponse)
async def execute_manifest(
    request: Request,
    manifest_id: str,
    typed_import_id: Annotated[str, Form()],
) -> HTMLResponse:
    feature = await _feature(request)
    if not feature["ready"]:
        return _status(
            request,
            error=feature["message"],
            error_code=feature["code"],
        )
    if not _valid_import_id(manifest_id):
        return _status(
            request,
            error="That manifest identifier is invalid.",
            error_code="OR-MANIFEST-NOT-FOUND",
        )
    connector = _connector(request)
    if connector is None:
        return _connector_required(request)
    try:
        manifest = await _load_manifest_page(feature["service"], manifest_id)
    except Exception as exc:  # noqa: BLE001
        code, message = _safe_error(exc, "OR-MANIFEST-NOT-FOUND")
        return _status(request, error=message, error_code=code)
    manifest_view = _manifest_record(manifest)
    if typed_import_id.strip() != str(manifest_view.get("import_id", "") or ""):
        return _manifest_response(
            request,
            manifest,
            task_error=(
                "OR-CONFIRMATION-MISMATCH",
                "Type the exact import ID shown on this immutable manifest.",
            ),
            progress=await _manifest_progress(feature["service"], manifest_id),
            gate=await _manifest_gate(feature["service"]),
        )
    if manifest_view["status"] not in {"planned", "awaiting_students", "paused"}:
        return _manifest_response(
            request,
            manifest,
            task_error=(
                "OR-MANIFEST-NOT-RUNNABLE",
                "This manifest is not awaiting an operator-confirmed execution.",
            ),
            progress=await _manifest_progress(feature["service"], manifest_id),
            gate=await _manifest_gate(feature["service"]),
        )

    state = request.app.state.gamgui
    tasks, _ = _task_maps(state)
    current = tasks.get(manifest_id)
    if current is None or current.done():
        _start_manifest_task(
            state,
            feature["service"],
            connector,
            manifest_id,
            typed_import_id.strip(),
        )
    return _manifest_response(
        request,
        manifest,
        notice="Execution was queued. Live state will be revalidated before GAM runs.",
        polling=True,
        progress=await _manifest_progress(feature["service"], manifest_id),
        gate=await _manifest_gate(feature["service"]),
    )


@router.get("/manifest/{manifest_id}/status", response_class=HTMLResponse)
async def manifest_status(
    request: Request,
    manifest_id: str,
    offset: int = 0,
) -> HTMLResponse:
    feature = await _feature(request)
    if not feature["ready"]:
        return _status(
            request,
            error=feature["message"],
            error_code=feature["code"],
        )
    if not _valid_import_id(manifest_id):
        return _status(
            request,
            error="That manifest identifier is invalid.",
            error_code="OR-MANIFEST-NOT-FOUND",
        )
    try:
        manifest = await _load_manifest_page(
            feature["service"], manifest_id, offset=offset
        )
    except Exception as exc:  # noqa: BLE001
        code, message = _safe_error(exc, "OR-MANIFEST-NOT-FOUND")
        return _status(request, error=message, error_code=code)
    tasks, errors = _task_maps(request.app.state.gamgui)
    task = tasks.get(manifest_id)
    status = str(_record(manifest).get("status", "") or "")
    polling = bool(
        (task is not None and not task.done())
        or status not in _MANIFEST_TERMINAL_STATES
        and status == "running"
    )
    return _manifest_response(
        request,
        manifest,
        task_error=errors.get(manifest_id),
        offset=offset,
        polling=polling,
        progress=await _manifest_progress(feature["service"], manifest_id),
        gate=await _manifest_gate(feature["service"]),
    )


@router.post("/manifest/{manifest_id}/pause", response_class=HTMLResponse)
async def pause_manifest(
    request: Request,
    manifest_id: str,
) -> HTMLResponse:
    feature = await _feature(request)
    if not feature["ready"]:
        return _status(
            request,
            error=feature["message"],
            error_code=feature["code"],
        )
    if not _valid_import_id(manifest_id):
        return _status(
            request,
            error="That manifest identifier is invalid.",
            error_code="OR-MANIFEST-NOT-FOUND",
        )
    service = feature["service"]
    try:
        pause = getattr(service, "request_execution_pause")
        await asyncio.to_thread(pause, manifest_id)
        manifest = await _load_manifest_page(service, manifest_id)
    except Exception as exc:  # noqa: BLE001
        code, message = _safe_error(exc, "OR-EXECUTION-NOT-RUNNING")
        manifest = await _load_manifest_page(service, manifest_id)
        return _manifest_response(
            request,
            manifest,
            task_error=(code, message),
            polling=True,
            progress=await _manifest_progress(service, manifest_id),
            gate=await _manifest_gate(service),
        )
    return _manifest_response(
        request,
        manifest,
        notice="Pause requested. The current batch will finish and verify before stopping.",
        polling=True,
        progress=await _manifest_progress(service, manifest_id),
        gate=await _manifest_gate(service),
    )


@router.post("/manifest/{manifest_id}/recover", response_class=HTMLResponse)
async def recover_manifest(
    request: Request,
    manifest_id: str,
) -> HTMLResponse:
    feature = await _feature(request)
    if not feature["ready"]:
        return _status(
            request,
            error=feature["message"],
            error_code=feature["code"],
        )
    if not _valid_import_id(manifest_id):
        return _status(
            request,
            error="That manifest identifier is invalid.",
            error_code="OR-MANIFEST-NOT-FOUND",
        )
    connector = _connector(request)
    if connector is None:
        return _connector_required(request)
    service = feature["service"]
    try:
        await _call(
            service,
            ("reconcile_interrupted_manifest",),
            (
                ((connector, manifest_id), {}),
                ((), {"connector": connector, "manifest_id": manifest_id}),
            ),
        )
        manifest = await _load_manifest_page(service, manifest_id)
    except Exception as exc:  # noqa: BLE001
        code, message = _safe_error(exc, "OR-RECOVERY-REQUIRED")
        manifest = await _load_manifest_page(service, manifest_id)
        return _manifest_response(
            request,
            manifest,
            task_error=(code, message),
            progress=await _manifest_progress(service, manifest_id),
            gate=await _manifest_gate(service),
        )
    return _manifest_response(
        request,
        manifest,
        notice=(
            "Interrupted batch outcomes were reconciled from live state. "
            "Review and confirm the verified remaining work before resuming."
        ),
        progress=await _manifest_progress(service, manifest_id),
        gate=await _manifest_gate(service),
    )


@router.post("/manifest/{manifest_id}/retry-stabilization", response_class=HTMLResponse)
async def retry_manifest_stabilization(
    request: Request,
    manifest_id: str,
) -> HTMLResponse:
    feature = await _feature(request)
    if not feature["ready"]:
        return _status(request, error=feature["message"], error_code=feature["code"])
    if not _valid_import_id(manifest_id):
        return _status(
            request,
            error="That manifest identifier is invalid.",
            error_code="OR-MANIFEST-NOT-FOUND",
        )
    connector = _connector(request)
    if connector is None:
        return _connector_required(request)
    service = feature["service"]
    try:
        await _call(
            service,
            ("retry_stabilization_read",),
            (
                ((connector, manifest_id), {}),
                ((), {"connector": connector, "manifest_id": manifest_id}),
            ),
        )
        manifest = await _load_manifest_page(service, manifest_id)
    except Exception as exc:  # noqa: BLE001
        code, message = _safe_error(exc, "OR-LIVE-NOT-STABLE")
        manifest = await _load_manifest_page(service, manifest_id)
        return _manifest_response(
            request,
            manifest,
            task_error=(code, message),
            progress=await _manifest_progress(service, manifest_id),
            gate=await _manifest_gate(service),
        )
    return _manifest_response(
        request,
        manifest,
        notice=(
            "Live reads are now stable. This stale approval remains read-only; "
            "build and approve a fresh plan before mutation."
        ),
        progress=await _manifest_progress(service, manifest_id),
        gate=await _manifest_gate(service),
    )


@router.get("/manifest/{manifest_id}/drift-report")
async def export_manifest_drift_report(
    request: Request,
    manifest_id: str,
) -> Response:
    feature = await _feature(request)
    if not feature["ready"]:
        return Response(feature["message"], status_code=503, media_type="text/plain")
    if not _valid_import_id(manifest_id):
        return Response("Manifest not found.", status_code=404, media_type="text/plain")
    try:
        manifest = _manifest_record(
            await _load_manifest_page(feature["service"], manifest_id)
        )
    except Exception:
        return Response("Manifest not found.", status_code=404, media_type="text/plain")
    payload = {
        "manifest_id": manifest.get("id", manifest_id),
        "import_id": manifest.get("import_id", ""),
        "manifest_hash": manifest.get("manifest_hash", ""),
        "status": manifest.get("status", ""),
        "error_code": manifest.get("error", ""),
        "drift": manifest.get("drift_report", []),
    }
    return Response(
        json.dumps(payload, indent=2, sort_keys=True),
        media_type="application/json",
        headers={
            "Content-Disposition": (
                f'attachment; filename="oneroster-drift-{manifest_id}.json"'
            )
        },
    )


@router.get("/gate", response_class=HTMLResponse)
async def student_gate(request: Request) -> HTMLResponse:
    feature = await _feature(request)
    if not feature["ready"]:
        return _status(
            request,
            error=feature["message"],
            error_code=feature["code"],
        )
    return TEMPLATES.TemplateResponse(
        request,
        "_oneroster_gate.html",
        {"gate": await _local_gate(feature["service"])},
    )


@router.post("/gate", response_class=HTMLResponse)
async def change_student_gate(
    request: Request,
    target_state: Annotated[str, Form()],
    manifest_id: Annotated[str, Form()] = "",
    manifest_hash: Annotated[str, Form()] = "",
    release_at: Annotated[str, Form()] = "",
    confirmation: Annotated[str, Form()] = "",
) -> HTMLResponse:
    feature = await _feature(request)
    if not feature["ready"]:
        return _status(
            request,
            error=feature["message"],
            error_code=feature["code"],
        )
    target = target_state.strip().upper()
    if target not in _GATE_STATES:
        return _status(
            request,
            error="Choose a valid student enrollment gate state.",
            error_code="OR-GATE-INVALID",
        )
    if target in {"ARMED", "OPEN"} and not _valid_import_id(manifest_id):
        return _status(
            request,
            error="Choose an approved manifest before arming or opening the gate.",
            error_code="OR-GATE-INVALID",
        )
    if target == "ARMED" and not _MANIFEST_HASH.fullmatch(
        manifest_hash.strip().casefold()
    ):
        return _status(
            request,
            error="The approved manifest hash is invalid.",
            error_code="OR-GATE-INVALID",
        )
    normalized_release = release_at.strip()
    if target == "ARMED":
        try:
            parsed_release = _chicago_aware(normalized_release)
            normalized_release = parsed_release.isoformat()
        except ValueError:
            return _status(
                request,
                error="Choose a valid America/Chicago release date and time.",
                error_code="OR-GATE-INVALID",
            )
    if target == "OPEN" and confirmation != "OPEN":
        return _status(
            request,
            error="Type OPEN exactly to release approved student changes.",
            error_code="OR-GATE-CONFIRMATION",
        )
    if target == "ARMED" and confirmation != "ARM":
        return _status(
            request,
            error="Type ARM exactly to bind the scheduled release.",
            error_code="OR-GATE-CONFIRMATION",
        )
    connector = _connector(request)
    if target == "OPEN" and connector is None:
        return _connector_required(request)
    payload = {
        "state": target,
        "manifest_id": manifest_id.strip(),
        "release_at": normalized_release,
        "timezone": "America/Chicago",
    }
    state = request.app.state.gamgui
    lease = None
    if target != "OPEN":
        lease = try_acquire_admin_activity(state, "oneroster-student-gate")
        if lease is None:
            return _status(
                request,
                error=ADMIN_ACTIVITY_BUSY_MESSAGE,
                error_code="CMP-ACTIVE-JOB",
            )
    try:
        if target == "CLOSED":
            value = await _call(
                feature["service"],
                ("close_gate", "set_student_gate", "change_gate", "set_gate"),
                (
                    ((), {}),
                    ((payload,), {}),
                    ((), payload),
                ),
            )
        elif target == "ARMED":
            value = await _call(
                feature["service"],
                ("arm_gate", "set_student_gate", "change_gate", "set_gate"),
                (
                    (
                        (
                            manifest_id.strip(),
                            manifest_hash.strip(),
                            normalized_release,
                        ),
                        {},
                    ),
                    (
                        (),
                        {
                            "manifest_id": manifest_id.strip(),
                            "manifest_hash": manifest_hash.strip(),
                            "release_at": normalized_release,
                        },
                    ),
                    ((payload,), {}),
                ),
            )
        else:
            # Revalidation owns the process-wide lease inside OneRosterExecutor.
            # Neither the manifest hash nor any current-live hash is accepted from
            # the browser for this transition.
            value = await _call(
                feature["service"],
                ("revalidate_scheduled_gate",),
                (
                    (
                        (connector, manifest_id.strip()),
                        {
                            "now": datetime.now(timezone.utc),
                            "manual": True,
                        },
                    ),
                    (
                        (),
                        {
                            "connector": connector,
                            "manifest_id": manifest_id.strip(),
                            "now": datetime.now(timezone.utc),
                            "manual": True,
                        },
                    ),
                ),
            )
    except Exception as exc:  # noqa: BLE001
        code, message = _safe_error(exc, "OR-GATE-INVALID")
        return _status(request, error=message, error_code=code)
    finally:
        if lease is not None:
            lease.release()
    execution_queued = False
    if target == "OPEN":
        execution_queued = _start_manifest_task(
            state,
            feature["service"],
            connector,
            manifest_id.strip(),
            "",
        )
    gate = await _local_gate(feature["service"])
    if not gate:
        gate = _record(value) or payload
    gate["state"] = str(_enum_value(gate.get("state", target)) or target)
    return TEMPLATES.TemplateResponse(
        request,
        "_oneroster_gate.html",
        {
            "gate": gate,
            "notice": (
                "Student enrollment gate is now OPEN. Approved student execution "
                "was queued."
                if target == "OPEN" and execution_queued
                else f"Student enrollment gate is now {target}."
            ),
        },
    )


@router.get("/history", response_class=HTMLResponse)
async def import_history(request: Request) -> HTMLResponse:
    feature = await _feature(request)
    if not feature["ready"]:
        return _status(
            request,
            error=feature["message"],
            error_code=feature["code"],
        )
    return TEMPLATES.TemplateResponse(
        request,
        "_oneroster_history.html",
        {"history": await _local_history(feature["service"])},
    )
