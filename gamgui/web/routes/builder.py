"""Command Builder + Sequencer (/builder).

Browse/search the categorized GAM command catalog; for the curated *buildable* commands, fill typed
slots (drag a user/group in), preview the exact `gam …` (argv assembled only via `GAMCommands` —
never shell), run it through the guard, and optionally chain commands into a sequence run as one
audited BatchJob. Browse-only commands are inert (read/copy syntax only).
"""

from __future__ import annotations

import asyncio
import csv
import io
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse

from ...core import guard as guard_mod
from ...core.catalog import load_catalog
from ...core.catalog.catalog import AREA_ORDER
from ...core.catalog.models import SlotKind
from ...core.connectors.base import ChangePreview, ConnectorID, RiskLevel
from ...core.gam.commands import GAMCommands
from ...core.gam.errors import GAMError
from ..jobs import start_job
from ..server import TEMPLATES

router = APIRouter(prefix="/builder")

MAX_SEQUENCE_STEPS = 25
INTERACTIVE_ROW_LIMIT = 50
INTERACTIVE_COLUMN_LIMIT = 8
INTERACTIVE_SCAN_BYTES = 512 * 1024
INTERACTIVE_CONTENT_CHARS = 8_000
INTERACTIVE_CELL_CHARS = 512
MAX_READ_INPUT_CHARS = 4_096

_SEQUENCE_PAGE = "_sequence.html"


def _st(request: Request):
    return request.app.state.gamgui


def _catalog(request: Request):
    st = _st(request)
    if st.catalog is None:
        st.catalog = load_catalog()
    return st.catalog


def _friendly(exc: Exception) -> str:
    return exc.remediation if isinstance(exc, GAMError) else "Something went wrong talking to GAM."


def _details(exc: Exception) -> str:
    """The raw error text to show under the friendly message — so 'see details below' has details."""
    if isinstance(exc, GAMError):
        return (exc.stderr or "").strip() or exc.message
    return str(exc)


def _err(request: Request, message: str, details: str = "") -> HTMLResponse:
    return TEMPLATES.TemplateResponse(request, "_action_result.html",
                                      {"ok": False, "message": message, "details": details})


# --- slot assembly ---------------------------------------------------------------------

async def _assemble(request: Request, cmd):
    """Read slot values from the form, validate, and build the argv via the curated builder.

    Returns (slots: dict, argv: list, target: str, error: str|None)."""
    form = await request.form()
    slots, target, read_input_chars = {}, "", 0
    for slot in cmd.slots:
        val = (form.get(slot.key) or "").strip()
        if slot.required and not val:
            return {}, [], "", f"{slot.label} is required."
        if cmd.risk == RiskLevel.READ_ONLY:
            read_input_chars += len(val)
            if read_input_chars > MAX_READ_INPUT_CHARS:
                return {}, [], "", "Read command inputs are too large for an interactive result."
        slots[slot.key] = val
        if not target and slot.kind in (SlotKind.TARGET_USER, SlotKind.USER, SlotKind.GROUP) and val:
            target = val
    try:
        argv = cmd.build(slots)
    except Exception as exc:  # noqa: BLE001 — a builder/validation error (e.g. bad role)
        return {}, [], "", str(exc)
    return slots, list(argv), (target or "(command)"), None


def _preview_of(cmd, argv, target) -> ChangePreview:
    return ChangePreview(connector_id=ConnectorID.GOOGLE_WORKSPACE, target=target,
                         summary=cmd.name, risk=cmd.risk, argv=argv)


def _gam_str(argv) -> str:
    return "gam " + " ".join(argv)


@dataclass(frozen=True)
class _BoundedRead:
    records: list[dict]
    output: str
    truncated: bool


def _coerce_records(value) -> list[dict]:
    if isinstance(value, dict):
        return [value]
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    return []


def _json_array_prefix(text: str, limit: int) -> tuple[list[dict], bool]:
    """Incrementally decode the beginning of a JSON array without loading the full array."""
    decoder = json.JSONDecoder()
    index = text.find("[") + 1
    records: list[dict] = []
    incomplete = False
    while index > 0:
        while index < len(text) and (text[index].isspace() or text[index] == ","):
            index += 1
        if index >= len(text):
            incomplete = True
            break
        if text[index] == "]":
            break
        try:
            value, index = decoder.raw_decode(text, index)
        except ValueError:
            incomplete = True
            break
        records.extend(_coerce_records(value))
        if len(records) > limit:
            break
    return records[: limit + 1], incomplete


def _tabular_prefix(text: str, limit: int) -> tuple[list[dict], bool]:
    """Parse at most ``limit + 1`` rows from a bounded text prefix."""
    stripped = text.lstrip()
    if stripped.startswith("["):
        return _json_array_prefix(stripped, limit)
    if stripped.startswith("{"):
        records: list[dict] = []
        ndjson = True
        for line in stripped.splitlines():
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except ValueError:
                ndjson = False
                break
            records.extend(_coerce_records(value))
            if len(records) > limit:
                break
        if ndjson:
            return records[: limit + 1], len(records) > limit
        try:
            return _coerce_records(json.loads(stripped))[: limit + 1], False
        except ValueError:
            return [], True

    first = stripped.splitlines()[0] if stripped else ""
    if "," not in first:
        return [], False
    reader = csv.DictReader(io.StringIO(stripped))
    records = []
    for row in reader:
        if "JSON" in row:
            try:
                nested = _coerce_records(json.loads(row.get("JSON") or ""))
            except ValueError:
                nested = []
            siblings = {
                key: value
                for key, value in row.items()
                if key is not None and key != "JSON" and value not in (None, "")
            }
            records.extend({**siblings, **record} for record in nested)
        else:
            records.append({key: value for key, value in row.items() if key is not None})
        if len(records) > limit:
            break
    return records[: limit + 1], len(records) > limit


def _display_value(value) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return str(value)


def _bound_records(records: list[dict]) -> tuple[list[dict], bool]:
    if not records:
        return [], False
    source_columns = list(records[0].keys())
    selected = source_columns[:INTERACTIVE_COLUMN_LIMIT]
    display_columns = [str(key)[:64] for key in selected]
    remaining = INTERACTIVE_CONTENT_CHARS
    bounded: list[dict] = []
    truncated = len(source_columns) > len(selected) or len(records) > INTERACTIVE_ROW_LIMIT
    for source in records[:INTERACTIVE_ROW_LIMIT]:
        row = {}
        for source_key, display_key in zip(selected, display_columns):
            if remaining <= 0:
                truncated = True
                break
            value = _display_value(source.get(source_key, ""))
            allowed = min(INTERACTIVE_CELL_CHARS, remaining)
            if len(value) > allowed:
                value = value[: max(0, allowed - 1)] + ("…" if allowed else "")
                truncated = True
            row[display_key] = value
            remaining -= len(value)
        if row:
            bounded.append(row)
        if remaining <= 0:
            break
    return bounded, truncated


def _read_bounded_spool(path: Path) -> _BoundedRead:
    """Reduce a private GAM spool to a response-safe payload.

    Only a fixed prefix is read, parsing is capped at one look-ahead row, and values are projected
    into a small global character budget. The caller runs this synchronous work in a worker thread.
    """
    source = Path(path)
    with source.open("rb") as fh:
        raw = fh.read(INTERACTIVE_SCAN_BYTES + 1)
    source_truncated = len(raw) > INTERACTIVE_SCAN_BYTES
    text = raw[:INTERACTIVE_SCAN_BYTES].decode("utf-8", "replace")
    records, parser_truncated = _tabular_prefix(text, INTERACTIVE_ROW_LIMIT)
    if records:
        bounded, value_truncated = _bound_records(records)
        return _BoundedRead(
            records=bounded,
            output="",
            truncated=source_truncated or parser_truncated or value_truncated,
        )
    clean = text.strip()
    output_truncated = len(clean) > INTERACTIVE_CONTENT_CHARS
    return _BoundedRead(
        records=[],
        output=clean[:INTERACTIVE_CONTENT_CHARS],
        truncated=source_truncated or parser_truncated or output_truncated,
    )


async def _run_bounded_read(conn, argv) -> _BoundedRead:
    async with conn.runner.run_authenticated_to_file(conn.domain, argv) as result:
        return await asyncio.to_thread(_read_bounded_spool, result.path)


def _render_read(request: Request, payload: _BoundedRead, gam: str) -> HTMLResponse:
    """Render an already bounded read payload; no parsing or large-buffer work runs on the loop."""
    context = {"gam": gam, "truncated": payload.truncated}
    if payload.records:
        return TEMPLATES.TemplateResponse(
            request, "_records_table.html", {**context, "records": payload.records}
        )
    return TEMPLATES.TemplateResponse(
        request, "_read_output.html", {**context, "output": payload.output}
    )


# --- pages -----------------------------------------------------------------------------

@router.get("", response_class=HTMLResponse)
async def page(request: Request) -> HTMLResponse:
    st = _st(request)
    if st.connector is None:
        return TEMPLATES.TemplateResponse(request, "builder.html", {"connected": False})
    cat = _catalog(request)
    counts = cat.area_counts()
    areas = [(a, counts[a]) for a in AREA_ORDER if counts.get(a)]
    # User/group lists aren't fetched here — the slot pickers query /builder/pick on demand so the
    # page loads instantly and the picker scales to large domains (only the cached top matches render).
    domain = getattr(st.connector, "domain", "") or st.audit_domain or ""
    return TEMPLATES.TemplateResponse(request, "builder.html", {
        "connected": True, "areas": areas, "sequence": st.builder_sequence, "row_actions": ROW_ACTIONS,
        "domain": domain,
    })


PICK_LIMIT = 25   # most matches a slot picker shows at once — keeps large domains snappy


@router.get("/pick", response_class=HTMLResponse)
async def pick(request: Request, kind: str = "users", q: str = "") -> HTMLResponse:
    """Type-ahead for a User/Group slot: search the cached directory, return at most PICK_LIMIT hits.

    Scales to large domains — the match runs against the persistent directory index shared with the
    Users list, and only the top matches are ever rendered, never the whole directory."""
    if not q:
        q = next(
            (
                value
                for key, value in request.query_params.items()
                if key not in {"kind", "q"}
            ),
            "",
        )
    st = _st(request)
    if st.connector is None:
        return TEMPLATES.TemplateResponse(
            request,
            "_picker_options.html",
            {"items": [], "more": False, "error": "Not connected."},
        )
    try:
        if kind == "groups":
            page = await st.directory_groups(query=q, limit=PICK_LIMIT)
            pairs = [(group.email, group.name) for group in page.items]
        else:
            page = await st.directory_users(query=q, scope="active", limit=PICK_LIMIT)
            pairs = [(user.primary_email, user.full_name) for user in page.items]
    except Exception:  # noqa: BLE001 — a directory hiccup just yields no suggestions
        return TEMPLATES.TemplateResponse(
            request,
            "_picker_options.html",
            {"items": [], "more": False, "error": "Directory search is temporarily unavailable."},
        )
    return TEMPLATES.TemplateResponse(
        request,
        "_picker_options.html",
        {"items": pairs, "more": page.total > len(page.items), "total": page.total},
    )


PAGE_SIZE = 6           # commands per page — sized so a page (2-line rows) fits a 13" window


def _paginated(request: Request, items, q="", page=1) -> HTMLResponse:
    total = len(items)
    pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    page = min(max(1, page), pages)
    start = (page - 1) * PAGE_SIZE
    return TEMPLATES.TemplateResponse(request, "_catalog_list.html", {
        "items": items[start:start + PAGE_SIZE], "q": q, "page": page, "total": total, "pages": pages,
        "has_prev": page > 1, "has_next": page < pages,
    })


@router.get("/catalog", response_class=HTMLResponse)
async def catalog_list(request: Request, area: str = "", q: str = "", buildable: str = "",
                       page: int = 1) -> HTMLResponse:
    """One flat, paginated list — search results, an area, or the buildable landing. No scroll box:
    each page is sized to fit, navigated with Prev/Next (the modern faceted-list pattern)."""
    cat = _catalog(request)
    q = q.strip()
    if q:
        items = cat.search(q)
    elif area:
        items = cat.in_area(area)
    else:
        items = cat.all_sorted()
    if buildable in ("1", "true", "on"):
        items = [c for c in items if c.buildable]
    return _paginated(request, items, q=q, page=page)


# Quick actions offered when you click an address in a result table. Each is a curated buildable
# command; `param` is the slot the clicked address pre-fills, `group` splits the menu (a result
# address may be a user or a group, so we offer both and let you pick the right one).
ROW_ACTIONS = [
    {"cid": "build.print_delegates", "label": "Delegates", "risk": "read", "param": "email", "group": "user"},
    {"cid": "build.print_forwarding", "label": "Forwarding", "risk": "read", "param": "email", "group": "user"},
    {"cid": "build.set_vacation", "label": "Set vacation", "risk": "change", "param": "email", "group": "user"},
    {"cid": "build.set_signature", "label": "Set signature", "risk": "change", "param": "email", "group": "user"},
    {"cid": "build.set_organization", "label": "Title / dept", "risk": "change", "param": "email", "group": "user"},
    {"cid": "build.reset_password", "label": "Reset password", "risk": "change", "param": "email", "group": "user"},
    {"cid": "build.suspend_user", "label": "Suspend", "risk": "destructive", "param": "email", "group": "user"},
    {"cid": "build.delete_user", "label": "Delete account", "risk": "destructive", "param": "email", "group": "user"},
    {"cid": "build.add_group_member", "label": "Add a member", "risk": "change", "param": "group", "group": "group"},
    {"cid": "build.remove_group_member", "label": "Remove a member", "risk": "change", "param": "group", "group": "group"},
]


@router.get("/command/{cid}", response_class=HTMLResponse)
async def command_form(request: Request, cid: str) -> HTMLResponse:
    cmd = _catalog(request).by_id(cid)
    if cmd is None:
        return _err(request, "Unknown command.")
    # Pre-fill any slot whose key is passed as a query param (e.g. ?email=alice@x.com from a row click).
    prefill = {k: v for k, v in request.query_params.items() if k != "cid"}
    return TEMPLATES.TemplateResponse(request, "_builder_form.html", {"cmd": cmd, "prefill": prefill})


# --- single-command preview + run ------------------------------------------------------

@router.post("/preview", response_class=HTMLResponse)
async def preview(request: Request, cid: Annotated[str, Form()]) -> HTMLResponse:
    cmd = _catalog(request).by_id(cid)
    if cmd is None or not cmd.buildable:
        return _err(request, "That command can't be built — copy its syntax and run it in GAM directly.")
    slots, argv, target, error = await _assemble(request, cmd)
    if error:
        return _err(request, error)
    decision = guard_mod.evaluate([_preview_of(cmd, argv, target)])
    return TEMPLATES.TemplateResponse(request, "_builder_preview.html", {
        "cmd": cmd, "gam": _gam_str(argv), "decision": decision, "target": target, "slots": slots,
    })


@router.post("/run", response_class=HTMLResponse)
async def run(request: Request, cid: Annotated[str, Form()]) -> HTMLResponse:
    st = _st(request)
    conn = st.connector
    if conn is None:
        return _err(request, "Not connected.")
    cmd = _catalog(request).by_id(cid)
    if cmd is None or not cmd.buildable:
        return _err(request, "That command can't be run from here.")
    slots, argv, target, error = await _assemble(request, cmd)
    if error:
        return _err(request, error)
    preview = _preview_of(cmd, argv, target)
    if cmd.risk == RiskLevel.READ_ONLY:
        form = await request.form()
        export = bool(form.get("td_export"))
        if export and not cmd.supports_export:
            return _err(request, "That command does not support Google Sheet export.")
        if export:  # send the result to a Google Sheet instead of the in-app table
            owner = (form.get("td_user") or "").strip()
            argv = argv + GAMCommands.todrive_args(owner, (form.get("td_title") or "").strip())
        try:
            payload = await _run_bounded_read(conn, argv)
        except Exception as exc:  # noqa: BLE001
            return _err(request, _friendly(exc), _details(exc))
        if export:
            return TEMPLATES.TemplateResponse(request, "_export_result.html",
                                              {"gam": _gam_str(argv),
                                               "output": payload.output or "GAM completed the export.",
                                               "owner": owner or "the admin account"})
        return _render_read(request, payload, _gam_str(argv))
    # A mutation that needs confirmation must come back through the preview (the "Confirm & run"
    # button sends confirmed=1) — a bare POST never silently runs a destructive command.
    decision = guard_mod.evaluate([preview])
    form = await request.form()
    if decision.requires_confirmation and not form.get("confirmed"):
        return TEMPLATES.TemplateResponse(request, "_builder_preview.html", {
            "cmd": cmd, "gam": _gam_str(argv), "decision": decision, "target": target, "slots": slots,
        })
    result = (await conn.apply([preview]))[0]
    return TEMPLATES.TemplateResponse(request, "_action_result.html",
                                      {"ok": result.ok, "message": (cmd.name + " — " + ("done" if result.ok else result.detail))})


# --- sequence --------------------------------------------------------------------------

@router.post("/sequence/add", response_class=HTMLResponse)
async def seq_add(request: Request, cid: Annotated[str, Form()]) -> HTMLResponse:
    st = _st(request)
    cmd = _catalog(request).by_id(cid)
    if cmd is None or not cmd.buildable:
        return _err(request, "That command can't be added.")
    if len(st.builder_sequence) >= MAX_SEQUENCE_STEPS:
        return _err(request, f"Sequence is capped at {MAX_SEQUENCE_STEPS} steps.")
    _, argv, target, error = await _assemble(request, cmd)
    if error:
        return _err(request, error)
    st.builder_sequence.append({
        "cid": cid, "label": cmd.name, "target": target, "argv": argv,
        "risk": int(cmd.risk), "gam": _gam_str(argv),
    })
    return TEMPLATES.TemplateResponse(request, _SEQUENCE_PAGE, {"sequence": st.builder_sequence})


@router.post("/sequence/remove", response_class=HTMLResponse)
async def seq_remove(request: Request, index: Annotated[int, Form()]) -> HTMLResponse:
    st = _st(request)
    if 0 <= index < len(st.builder_sequence):
        st.builder_sequence.pop(index)
    return TEMPLATES.TemplateResponse(request, _SEQUENCE_PAGE, {"sequence": st.builder_sequence})


@router.post("/sequence/move", response_class=HTMLResponse)
async def seq_move(request: Request, index: Annotated[int, Form()], to: Annotated[int, Form()]) -> HTMLResponse:
    st = _st(request)
    seq = st.builder_sequence
    if 0 <= index < len(seq) and 0 <= to < len(seq):
        seq.insert(to, seq.pop(index))
    return TEMPLATES.TemplateResponse(request, _SEQUENCE_PAGE, {"sequence": seq})


@router.post("/sequence/clear", response_class=HTMLResponse)
async def seq_clear(request: Request) -> HTMLResponse:
    _st(request).builder_sequence.clear()
    return TEMPLATES.TemplateResponse(request, _SEQUENCE_PAGE, {"sequence": []})


def _seq_previews(seq) -> list:
    return [ChangePreview(connector_id=ConnectorID.GOOGLE_WORKSPACE, target=s["target"],
                          summary=s["label"], risk=RiskLevel(s["risk"]), argv=s["argv"]) for s in seq]


@router.post("/sequence/preview", response_class=HTMLResponse)
async def seq_preview(request: Request) -> HTMLResponse:
    st = _st(request)
    if not st.builder_sequence:
        return _err(request, "The sequence is empty.")
    decision = guard_mod.evaluate(_seq_previews(st.builder_sequence))
    return TEMPLATES.TemplateResponse(request, "_sequence_preview.html",
                                      {"sequence": st.builder_sequence, "decision": decision})


async def _run_sequence(job, conn, previews) -> None:
    try:
        for p in previews:
            job.current = p.summary
            try:
                res = (await conn.apply([p]))[0]
                ok, detail = res.ok, res.detail
            except Exception as exc:  # noqa: BLE001 — report every step, never abort the run
                ok, detail = False, str(exc)
            line = f"{p.summary} — {p.target}" + (f": {detail}" if (not ok and detail) else "")
            job.log.append(("✓ " if ok else "✗ ") + line)
            (job.__setattr__("applied", job.applied + 1) if ok else job.failed.append(f"{p.summary} ({p.target})"))
            job.done += 1
    finally:
        job.current = ""
        job.finished = True


@router.post("/sequence/run", response_class=HTMLResponse)
async def seq_run(request: Request, confirm: Annotated[str, Form()] = "", confirmed: Annotated[str, Form()] = "") -> HTMLResponse:
    st = _st(request)
    conn = st.connector
    if conn is None:
        return _err(request, "Not connected.")
    seq = list(st.builder_sequence)
    if not seq:
        return _err(request, "The sequence is empty.")
    previews = _seq_previews(seq)
    decision = guard_mod.evaluate(previews)
    # Enforce the full guard server-side (mirrors /run): a bulk-destructive sequence needs typed
    # "confirm"; any other confirmation-requiring sequence needs the Confirm & run click.
    def _needs_confirm(msg: str = "") -> HTMLResponse:
        return TEMPLATES.TemplateResponse(request, "_sequence_preview.html",
                                          {"sequence": seq, "decision": decision, "error": msg})
    if decision.requires_typed_confirmation:
        if confirm.strip().lower() != "confirm":
            return _needs_confirm("Type confirm to run this destructive bulk sequence.")
    elif decision.requires_confirmation and not confirmed:
        return _needs_confirm()
    job = start_job(st.jobs, len(previews))
    job.task = asyncio.create_task(_run_sequence(job, conn, previews))
    return TEMPLATES.TemplateResponse(request, "_sequence_run.html", {"job": job})


@router.get("/sequence/status", response_class=HTMLResponse)
async def seq_status(request: Request, job: str = "") -> HTMLResponse:
    j = _st(request).jobs.get(job)
    if j is None:
        return _err(request, "That run is no longer available.")
    return TEMPLATES.TemplateResponse(request, "_sequence_run.html", {"job": j})
