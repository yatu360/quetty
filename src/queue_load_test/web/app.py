"""FastAPI application for local Queue-it staging operations."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qs
from uuid import uuid4

from fastapi import FastAPI, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import HttpUrl, TypeAdapter, ValidationError

from queue_load_test.config import Settings
from queue_load_test.models import BrowserRuntimeState, QueueStatus, RunConfig
from queue_load_test.repository import RepositoryError, SessionRepository
from queue_load_test.web.manual import ManualOpenError
from queue_load_test.web.service import (
    ApplicationRunRuntime,
    DashboardService,
    RunRuntime,
)

_WEB_ROOT = Path(__file__).parent


def create_app(
    *,
    settings: Settings,
    repository: SessionRepository,
    runtime: RunRuntime | None = None,
) -> FastAPI:
    run_runtime = runtime or ApplicationRunRuntime(settings=settings, repository=repository)
    dashboard = DashboardService(repository, run_runtime)
    templates = Jinja2Templates(directory=_WEB_ROOT / "templates")

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        await repository.initialize()
        active = await repository.get_active_run()
        if active is not None:
            await run_runtime.start_run(active)
        try:
            yield
        finally:
            await run_runtime.close()
            await repository.close()

    app = FastAPI(title="Queue Session Operator", lifespan=lifespan)
    app.mount("/static", StaticFiles(directory=_WEB_ROOT / "static"), name="static")

    @app.get("/", response_class=HTMLResponse)
    async def index() -> RedirectResponse:
        target = "/dashboard" if await repository.get_active_run() is not None else "/setup"
        return RedirectResponse(target, status_code=303)

    @app.get("/setup", response_class=HTMLResponse)
    async def setup_page(request: Request) -> Response:
        if await repository.get_active_run() is not None:
            return RedirectResponse("/dashboard", status_code=303)
        return templates.TemplateResponse(
            request,
            "setup.html",
            {"error": None, "target_url": "", "requested_sessions": ""},
        )

    @app.post("/setup", response_class=HTMLResponse)
    async def submit_setup(request: Request) -> Response:
        if await repository.get_active_run() is not None:
            return RedirectResponse("/dashboard", status_code=303)
        form = parse_qs((await request.body()).decode("utf-8"), keep_blank_values=True)
        raw_url = form.get("target_url", [""])[0].strip()
        raw_count = form.get("requested_sessions", [""])[0].strip()
        error: str | None = None
        try:
            validated_url = TypeAdapter(HttpUrl).validate_python(raw_url)
            if validated_url.username is not None or validated_url.password is not None:
                raise ValueError("Target URL credentials are not allowed")
            target_url = str(validated_url)
        except (ValidationError, ValueError):
            error = "Enter a valid absolute HTTP or HTTPS target URL."
            target_url = raw_url
        try:
            requested_sessions = int(raw_count)
        except ValueError:
            requested_sessions = 0
            error = error or "Requested sessions must be a whole number."
        if requested_sessions < 1:
            error = error or "Requested sessions must be at least 1."
        elif requested_sessions > settings.max_manual_requested_sessions:
            error = (
                f"Requested sessions cannot exceed the configured safety limit of "
                f"{settings.max_manual_requested_sessions}."
            )
        if error is not None:
            return templates.TemplateResponse(
                request,
                "setup.html",
                {
                    "error": error,
                    "target_url": raw_url,
                    "requested_sessions": raw_count,
                },
                status_code=422,
            )
        run = RunConfig(
            run_id=str(uuid4()),
            target_url=target_url,
            requested_sessions=requested_sessions,
            created_at=datetime.now(UTC),
        )
        try:
            await repository.create_run(run)
        except RepositoryError as exc:
            return templates.TemplateResponse(
                request,
                "setup.html",
                {
                    "error": str(exc),
                    "target_url": raw_url,
                    "requested_sessions": raw_count,
                },
                status_code=409,
            )
        await run_runtime.start_run(run)
        return RedirectResponse("/dashboard", status_code=303)

    async def require_run() -> RunConfig | None:
        return await repository.get_active_run()

    @app.get("/dashboard", response_class=HTMLResponse)
    async def dashboard_page(
        request: Request,
        page: int = Query(1, ge=1),
        search: str = "",
        status: str = "",
        runtime_state: str = "",
    ) -> Response:
        run = await require_run()
        if run is None:
            return RedirectResponse("/setup", status_code=303)
        session_page = await _session_page(repository, page, search, status, runtime_state)
        return templates.TemplateResponse(
            request,
            "dashboard.html",
            {
                "summary": await dashboard.summary(run),
                "session_page": session_page,
                "search": search,
                "selected_status": status,
                "selected_runtime": runtime_state,
                "statuses": tuple(QueueStatus),
                "runtime_states": tuple(BrowserRuntimeState),
                "action_error": None,
                "action_message": None,
            },
        )

    @app.get("/partials/summary", response_class=HTMLResponse)
    async def summary_partial(request: Request) -> Response:
        run = await require_run()
        if run is None:
            return RedirectResponse("/setup", status_code=303)
        return templates.TemplateResponse(
            request,
            "_summary.html",
            {"summary": await dashboard.summary(run)},
        )

    @app.post("/monitoring/pause", response_class=HTMLResponse)
    async def pause_monitoring(request: Request) -> Response:
        run = await require_run()
        if run is None:
            return RedirectResponse("/setup", status_code=303)
        await run_runtime.pause_monitoring()
        return templates.TemplateResponse(
            request,
            "_summary.html",
            {"summary": await dashboard.summary(run)},
        )

    @app.post("/monitoring/resume", response_class=HTMLResponse)
    async def resume_monitoring(request: Request) -> Response:
        run = await require_run()
        if run is None:
            return RedirectResponse("/setup", status_code=303)
        await run_runtime.resume_monitoring()
        return templates.TemplateResponse(
            request,
            "_summary.html",
            {"summary": await dashboard.summary(run)},
        )

    @app.get("/partials/sessions", response_class=HTMLResponse)
    async def sessions_partial(
        request: Request,
        page: int = Query(1, ge=1),
        search: str = "",
        status: str = "",
        runtime_state: str = "",
    ) -> Response:
        if await require_run() is None:
            return RedirectResponse("/setup", status_code=303)
        return templates.TemplateResponse(
            request,
            "_sessions.html",
            {
                "session_page": await _session_page(
                    repository, page, search, status, runtime_state
                ),
                "search": search,
                "selected_status": status,
                "selected_runtime": runtime_state,
                "action_error": None,
                "action_message": None,
            },
        )

    @app.post("/sessions/{session_id}/open", response_class=HTMLResponse)
    async def open_session(
        request: Request,
        session_id: str,
        page: int = Query(1, ge=1),
        search: str = "",
        status: str = "",
        runtime_state: str = "",
    ) -> Response:
        if await require_run() is None:
            return RedirectResponse("/setup", status_code=303)
        message: str | None = None
        error: str | None = None
        try:
            message = (await run_runtime.open_session(session_id)).message
        except (ManualOpenError, RepositoryError) as exc:
            error = str(exc)
        return templates.TemplateResponse(
            request,
            "_sessions.html",
            {
                "session_page": await _session_page(
                    repository, page, search, status, runtime_state
                ),
                "search": search,
                "selected_status": status,
                "selected_runtime": runtime_state,
                "action_error": error,
                "action_message": message,
            },
        )

    @app.post("/sessions/{session_id}/close", response_class=HTMLResponse)
    async def close_session(
        request: Request,
        session_id: str,
        page: int = Query(1, ge=1),
        search: str = "",
        status: str = "",
        runtime_state: str = "",
    ) -> Response:
        if await require_run() is None:
            return RedirectResponse("/setup", status_code=303)
        closed = await run_runtime.close_session(session_id)
        return templates.TemplateResponse(
            request,
            "_sessions.html",
            {
                "session_page": await _session_page(
                    repository, page, search, status, runtime_state
                ),
                "search": search,
                "selected_status": status,
                "selected_runtime": runtime_state,
                "action_error": None if closed else "Session is not open in Chrome",
                "action_message": "Chrome session closed" if closed else None,
            },
        )

    return app


async def _session_page(
    repository: SessionRepository,
    page: int,
    search: str,
    status: str,
    runtime_state: str,
) -> object:
    parsed_status = QueueStatus.parse(status) if status else None
    parsed_runtime = BrowserRuntimeState(runtime_state) if runtime_state else None
    return await repository.list_session_summaries(
        page=page,
        page_size=50,
        search=search or None,
        status=parsed_status,
        runtime_state=parsed_runtime,
    )
