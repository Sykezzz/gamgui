"""Synthetic, non-tenant Classroom server for browser-level functional exercise."""

from pathlib import Path
import tempfile
from types import SimpleNamespace

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from gamgui.core.classroom.index import CourseIndex
from gamgui.core.classroom.manifests import RosterManifestStore
from gamgui.core.classroom.models import CourseSummary
from gamgui.web.routes.classroom import router as classroom_router
from gamgui.web.routes.oneroster import router as oneroster_router
from tests.classroom_fakes import FakeClassroomConnector
from tests.test_oneroster_web import FakeComponentManager, FakeOneRosterService


def create_preview_app(root: Path) -> FastAPI:
    root.mkdir(parents=True, exist_ok=True)
    connector = FakeClassroomConnector()
    index = CourseIndex(root / "courses.db")
    index.replace_all(
        "example.com",
        [
            CourseSummary(
                id=str(number),
                name=f"Course {number:03d}",
                section=f"Section_{number:03d}",
                room=f"R{number % 10}",
                owner_id=f"owner-{number % 4}",
                course_state="ACTIVE",
            )
            for number in range(75)
        ],
    )
    oneroster = FakeOneRosterService()
    oneroster.snapshots["import-1"].update(
        state="ready",
        ready_for_apply=True,
        blocking_issue_count=0,
        counts={
            "students": 18_420,
            "teachers": 812,
            "enrollments": 52_210,
            "ready_courses": 214,
            "deferred_courses": 18,
        },
    )
    state = SimpleNamespace(
        connector=connector,
        audit_domain="example.com",
        classroom_index=index,
        classroom_manifests=RosterManifestStore(root / "operations.db"),
        component_manager=FakeComponentManager(),
        oneroster_service=oneroster,
        oneroster_manifest_tasks={},
        oneroster_manifest_errors={},
    )
    app = FastAPI()
    app.state.gamgui = state

    @app.get("/health")
    async def health() -> dict[str, bool]:
        return {"ok": True}

    app.mount("/static", StaticFiles(directory="gamgui/web/static"), name="static")
    app.include_router(classroom_router)
    app.include_router(oneroster_router)

    @app.get("/components/status", response_class=HTMLResponse)
    async def component_status(context: str = "") -> str:
        del context
        return '<a href="/classroom/imports">Open guided import</a>'

    async def placeholder(request: Request) -> HTMLResponse:
        label = request.url.path.strip("/").replace("-", " ").title() or "Home"
        return HTMLResponse(f"<main><h1>{label}</h1><p>Synthetic functional preview.</p></main>")

    for path in (
        "/",
        "/users",
        "/groups",
        "/signatures",
        "/calendars",
        "/builder",
        "/onboard",
        "/lifecycle",
        "/reports",
        "/audit",
        "/components",
        "/classroom/access",
    ):
        app.add_api_route(path, placeholder, methods=["GET"], response_class=HTMLResponse)
    return app


if __name__ == "__main__":
    uvicorn.run(
        create_preview_app(Path(tempfile.gettempdir()) / "gamgui-classroom-preview"),
        host="127.0.0.1",
        port=8771,
        log_level="warning",
    )
