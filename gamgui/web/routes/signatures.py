"""Signature designer routes: scoped template -> preview -> apply (with live progress)."""

from __future__ import annotations

import asyncio
import secrets
from dataclasses import dataclass, field
from typing import Annotated, List, Optional

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse

from ...core import signatures as sig
from ...core.gam.commands import SIGNATURE_USER_FIELDS
from ...core.gam.errors import GAMError
from ...core.signatures import SignatureStore
from ..server import TEMPLATES

router = APIRouter(prefix="/signatures")

_SIGNATURES_PAGE = "signatures.html"
_PREVIEW_PARTIAL = "_sig_preview.html"
_APPLY_PARTIAL = "_sig_apply.html"
_TEMPLATES_PARTIAL = "_sig_templates.html"
_SCOPE_OPTIONS_PARTIAL = "_sig_scope_options.html"
_SCOPE_LIMIT = 50
_SCOPE_TYPES = frozenset({"company", "user", "group", "ou", "department", "location"})


def _store(request: Request) -> SignatureStore:
    """The saved-template store, lazily created on first use (real ~/Library file unless a test
    pre-seeds ``st.sig_templates`` with a store pointed at a tmp path)."""
    st = request.app.state.gamgui
    if st.sig_templates is None:
        st.sig_templates = SignatureStore()
    return st.sig_templates


def _tctx(store: SignatureStore, **extra) -> dict:
    """Context for ``_sig_templates.html``: each template as {name, body}, plus any saved/error flag."""
    ctx: dict = {"templates": [{"name": n, "body": store.get(n)} for n in store.names()]}
    ctx.update(extra)
    return ctx


@dataclass
class SigResult:
    """One user's outcome in a bulk apply — the unit of the live "as they get set" feed."""

    email: str
    ok: bool


_RECENT_WINDOW = 12       # most-recent per-user results kept for the live feed (bounds the polled HTML)
_FAILED_SAMPLE_CAP = 200  # cap the retained failed-email list so a mostly-failing run can't bloat the poll


@dataclass
class ApplyJob:
    """In-memory progress for one bulk signature apply, polled by the UI."""

    id: str
    total: int
    applied: int = 0
    done: int = 0
    failed_total: int = 0
    failed: List[str] = field(default_factory=list)         # capped sample of failed emails (see _FAILED_SAMPLE_CAP)
    recent: List[SigResult] = field(default_factory=list)   # rolling window, newest last; drives the live feed
    current: str = ""
    finished: bool = False
    error: Optional[str] = None
    task: object = field(default=None, repr=False)  # strong ref so the bg task isn't GC'd mid-run

    def record(self, email: str, ok: bool) -> None:
        """Log one user's outcome: tallies, the capped failed sample, and the rolling live feed.

        Both the failed list and the feed are bounded so the polled status partial stays small even
        on a domain-wide (thousands of users) apply — the feed shows only the most recent handful.
        """
        if ok:
            self.applied += 1
        else:
            self.failed_total += 1
            if len(self.failed) < _FAILED_SAMPLE_CAP:
                self.failed.append(email)
        self.recent.append(SigResult(email, ok))
        if len(self.recent) > _RECENT_WINDOW:
            del self.recent[0]
        self.done += 1


def _friendly(exc: Exception) -> str:
    return exc.remediation if isinstance(exc, GAMError) else "Something went wrong talking to GAM."


def _validated_scope(scope_type: str, scope_value: str) -> tuple[str, str]:
    """Normalize and validate every signature scope before any directory or GAM read."""

    scope = (scope_type or "").strip().lower()
    value = (scope_value or "").strip()
    if scope not in _SCOPE_TYPES:
        raise ValueError("Choose a supported signature scope.")
    if scope == "company":
        if value:
            raise ValueError("Whole-company scope does not accept a scope value.")
        return scope, ""
    if not value:
        noun = {
            "user": "specific user",
            "group": "group",
            "ou": "org unit",
            "department": "department",
            "location": "location",
        }[scope]
        raise ValueError(
            f"No active users match this scope. Choose a {noun} before previewing or applying."
        )
    if any(ord(char) < 32 for char in value):
        raise ValueError("Signature scope values cannot contain control characters.")
    if scope in {"user", "group"}:
        local, separator, domain = value.partition("@")
        if (
            not separator
            or not local
            or not domain
            or "@" in domain
            or len(value) > 254
            or any(char.isspace() for char in value)
        ):
            raise ValueError(
                f"Enter a valid {scope} email address for this signature scope."
            )
    elif scope == "ou":
        if not value.startswith("/") or len(value) > 512:
            raise ValueError("Enter a valid org unit path beginning with /.")
    elif len(value) > 256:
        raise ValueError(f"{scope.title()} scope values must be 256 characters or fewer.")
    return scope, value


async def _matched(st, scope_type: str, scope_value: str):
    """Resolve active users without touching the legacy tenant-wide AppState cache."""

    if scope_type == "user":
        user = await st.connector.get_user(
            scope_value,
            fields=SIGNATURE_USER_FIELDS,
        )
        return sig.match_scope([user], "user", scope_value)
    return await st.connector.list_signature_scope_users(scope_type, scope_value)


def _prune_jobs(st, keep: int = 10) -> None:
    """Drop the oldest finished jobs so the registry can't grow without bound."""
    finished = [jid for jid, j in st.jobs.items() if j.finished]
    for jid in finished[:-keep] if len(finished) > keep else []:
        st.jobs.pop(jid, None)


async def _run_apply(job: ApplyJob, conn, matched, template: str) -> None:
    """Background task: set each user's signature, updating ``job`` as it goes."""
    try:
        for u in matched:
            job.current = u.primary_email
            try:
                result = await conn.set_signature(u.primary_email, sig.render_signature(template, u), html=True)
                ok = bool(getattr(result, "ok", False))
            except Exception:
                ok = False
            job.record(u.primary_email, ok)
    except Exception as exc:  # whole-batch failure (e.g. auth expired mid-run)
        job.error = _friendly(exc)
    finally:
        job.current = ""
        job.finished = True


@router.get("", response_class=HTMLResponse)
async def page(request: Request) -> HTMLResponse:
    st = request.app.state.gamgui
    if st.connector is None:
        return TEMPLATES.TemplateResponse(request, _SIGNATURES_PAGE, {"connected": False})
    return TEMPLATES.TemplateResponse(
        request, _SIGNATURES_PAGE,
        {"connected": True, "variables": sig.VARIABLES},
    )


@router.get("/scopes", response_class=HTMLResponse)
async def search_scopes(
    request: Request,
    scope_type: str = "user",
    q: str = "",
) -> HTMLResponse:
    """Search one bounded page of directory-backed signature scope options."""
    st = request.app.state.gamgui
    if st.connector is None:
        return TEMPLATES.TemplateResponse(
            request,
            _SCOPE_OPTIONS_PARTIAL,
            {"items": [], "error": "Not connected."},
        )
    scope_type = scope_type.strip().lower()
    query = q.strip()
    if scope_type not in _SCOPE_TYPES:
        return TEMPLATES.TemplateResponse(
            request,
            _SCOPE_OPTIONS_PARTIAL,
            {"items": [], "error": "Choose a supported signature scope."},
            status_code=400,
        )
    if scope_type == "company":
        return TEMPLATES.TemplateResponse(
            request,
            _SCOPE_OPTIONS_PARTIAL,
            {"items": [], "scope_type": scope_type},
        )
    if scope_type == "location":
        return TEMPLATES.TemplateResponse(
            request,
            _SCOPE_OPTIONS_PARTIAL,
            {"items": [], "free_text": True, "scope_type": scope_type},
        )
    try:
        if scope_type == "group":
            page = await st.directory_groups(query=query, limit=_SCOPE_LIMIT)
            items = [
                (group.email, group.name or group.email, group.email)
                for group in page.items
            ]
        else:
            page = await st.directory_users(
                query=query,
                scope="active",
                limit=_SCOPE_LIMIT,
            )
            if scope_type == "user":
                items = [
                    (user.primary_email, user.full_name or user.primary_email, user.primary_email)
                    for user in page.items
                ]
            else:
                attr = "org_unit_path" if scope_type == "ou" else "department"
                values = sorted(
                    {
                        str(getattr(user, attr, "") or "").strip()
                        for user in page.items
                        if str(getattr(user, attr, "") or "").strip()
                    },
                    key=str.casefold,
                )
                items = [(value, value, "") for value in values[:_SCOPE_LIMIT]]
    except Exception as exc:
        return TEMPLATES.TemplateResponse(
            request,
            _SCOPE_OPTIONS_PARTIAL,
            {"items": [], "error": _friendly(exc)},
        )
    return TEMPLATES.TemplateResponse(
        request,
        _SCOPE_OPTIONS_PARTIAL,
        {
            "items": items,
            "more": page.total > len(page.items),
            "total": page.total,
            "scope_type": scope_type,
        },
    )


@router.post("/preview", response_class=HTMLResponse)
async def preview(
    request: Request,
    template: Annotated[str, Form()] = "",
    scope_type: Annotated[str, Form()] = "company",
    scope_value: Annotated[str, Form()] = "",
) -> HTMLResponse:
    st = request.app.state.gamgui
    if st.connector is None:
        return TEMPLATES.TemplateResponse(request, _PREVIEW_PARTIAL, {"error": "Not connected."})
    try:
        scope_type, scope_value = _validated_scope(scope_type, scope_value)
        matched = await _matched(st, scope_type, scope_value)
    except ValueError as exc:
        return TEMPLATES.TemplateResponse(request, _PREVIEW_PARTIAL, {"error": str(exc)})
    except Exception as exc:
        return TEMPLATES.TemplateResponse(request, _PREVIEW_PARTIAL, {"error": _friendly(exc)})
    sample = matched[0] if matched else None
    return TEMPLATES.TemplateResponse(
        request, _PREVIEW_PARTIAL,
        {"rendered": sig.render_signature(template, sample) if sample else "", "count": len(matched),
         "sample": sample, "warning": sig.smart_quote_warning(template)},
    )


@router.post("/apply", response_class=HTMLResponse)
async def apply(
    request: Request,
    template: Annotated[str, Form()] = "",
    scope_type: Annotated[str, Form()] = "company",
    scope_value: Annotated[str, Form()] = "",
) -> HTMLResponse:
    st = request.app.state.gamgui
    if st.connector is None:
        return TEMPLATES.TemplateResponse(request, _APPLY_PARTIAL, {"error": "Not connected."})
    try:
        scope_type, scope_value = _validated_scope(scope_type, scope_value)
        matched = await _matched(st, scope_type, scope_value)
    except ValueError as exc:
        return TEMPLATES.TemplateResponse(request, _APPLY_PARTIAL, {"error": str(exc)})
    except Exception as exc:
        return TEMPLATES.TemplateResponse(request, _APPLY_PARTIAL, {"error": _friendly(exc)})
    if not matched:
        return TEMPLATES.TemplateResponse(request, _APPLY_PARTIAL, {"error": "No active users match this scope."})

    # Run the (potentially minutes-long) per-user loop in the background and report progress by polling,
    # so the UI never looks frozen on a large apply.
    job = ApplyJob(id=secrets.token_urlsafe(8), total=len(matched))
    _prune_jobs(st)
    st.jobs[job.id] = job
    job.task = asyncio.create_task(_run_apply(job, st.connector, matched, template))
    return TEMPLATES.TemplateResponse(request, _APPLY_PARTIAL, {"job": job})


@router.get("/apply/status", response_class=HTMLResponse)
async def apply_status(request: Request, job: str = "") -> HTMLResponse:
    st = request.app.state.gamgui
    j = st.jobs.get(job)
    if j is None:
        return TEMPLATES.TemplateResponse(request, _APPLY_PARTIAL, {"error": "That apply job is no longer available — re-run apply."})
    return TEMPLATES.TemplateResponse(request, _APPLY_PARTIAL, {"job": j})


# --- saved templates: load / save-as / delete ----------------------------------------------------

@router.get("/templates", response_class=HTMLResponse)
async def list_templates(request: Request) -> HTMLResponse:
    return TEMPLATES.TemplateResponse(request, _TEMPLATES_PARTIAL, _tctx(_store(request)))


@router.post("/templates/save", response_class=HTMLResponse)
async def save_template(
    request: Request,
    name: Annotated[str, Form()] = "",
    template: Annotated[str, Form()] = "",
) -> HTMLResponse:
    # `template` rides in via hx-include="#sig-form" — the CURRENT editor content — while `name`
    # comes from the save form's own input.
    store = _store(request)
    try:
        store.save(name, template)
    except ValueError as exc:
        return TEMPLATES.TemplateResponse(request, _TEMPLATES_PARTIAL, _tctx(store, error=str(exc)))
    return TEMPLATES.TemplateResponse(request, _TEMPLATES_PARTIAL, _tctx(store, saved=name.strip()))


@router.post("/templates/delete", response_class=HTMLResponse)
async def delete_template(request: Request, name: Annotated[str, Form()] = "") -> HTMLResponse:
    store = _store(request)
    store.delete(name)
    return TEMPLATES.TemplateResponse(request, _TEMPLATES_PARTIAL, _tctx(store))
