"""FastAPI application for local Queue-it staging operations."""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from html import escape
from pathlib import Path
from urllib.parse import parse_qs
from uuid import uuid4

from fastapi import FastAPI, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from markupsafe import Markup
from prometheus_client import CONTENT_TYPE_LATEST
from pydantic import HttpUrl, TypeAdapter, ValidationError

from queue_load_test.browser import CAMOUFOX_BROWSER_VERSION
from queue_load_test.browser.patchright_preflight import (
    PatchrightPreflight,
    run_patchright_preflight,
)
from queue_load_test.browser.preflight import CamoufoxPreflight, run_camoufox_preflight
from queue_load_test.config import Settings
from queue_load_test.metrics.logging import log_event
from queue_load_test.models import (
    BrowserBackendName,
    BrowserRuntimeState,
    MonitoringStrategy,
    ProxyProvider,
    QueueStatus,
    RunConfig,
    SessionSummaryPage,
)
from queue_load_test.repository import RepositoryError, SessionRepository
from queue_load_test.utils.instance_lock import InstanceLock
from queue_load_test.web.actions import OperatorActionKind, OperatorActionStatus
from queue_load_test.web.manual import ManualOpenError
from queue_load_test.web.service import (
    ApplicationRunRuntime,
    DashboardService,
    RunRuntime,
)

_WEB_ROOT = Path(__file__).parent
_PAGE_SIZE = 50
_SHUTTING_DOWN = "Application is shutting down; the action was not accepted."
logger = logging.getLogger(__name__)


def create_app(
    *,
    settings: Settings,
    repository: SessionRepository,
    runtime: RunRuntime | None = None,
    instance_lock: InstanceLock | None = None,
    camoufox_preflight: CamoufoxPreflight | None = None,
    patchright_preflight: PatchrightPreflight | None = None,
) -> FastAPI:
    """Build the operator UI.

    ``instance_lock`` proves this is the only UI process for the database; startup
    then clears every persisted browser owner (all belong to dead processes).
    Without it only expired ownership is recovered.

    Backend-specific preflights run before a new Camoufox or Patchright run is
    persisted. Existing runs never re-run them: they restart with their persisted
    backend.
    """

    camoufox_ready = camoufox_preflight or run_camoufox_preflight
    patchright_ready = patchright_preflight or run_patchright_preflight

    run_runtime = runtime or ApplicationRunRuntime(settings=settings, repository=repository)
    dashboard = DashboardService(repository, run_runtime)
    templates = Jinja2Templates(directory=_WEB_ROOT / "templates")
    templates.env.filters["local_time"] = local_time_markup
    lifecycle = {"accepting": False, "resetting": False, "stopping": False}

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        if instance_lock is not None:
            instance_lock.acquire()
        try:
            await repository.initialize()
            recovery = await repository.recover_startup_ownership(
                now=datetime.now(UTC),
                exclusive=instance_lock is not None and instance_lock.held,
            )
            log_event(
                logger,
                logging.INFO,
                "startup_ownership_recovered",
                count=recovery.manual_released,
                recovered_leases=recovery.leases_released,
            )
            active = await repository.get_active_run()
            if active is not None:
                await run_runtime.start_run(active)
            lifecycle["accepting"] = True
            try:
                yield
            finally:
                # Uvicorn has stopped accepting connections and finished in-flight
                # requests; refuse anything still arriving before tearing down.
                lifecycle["stopping"] = True
                lifecycle["accepting"] = False
                run_runtime.stop_accepting()
                try:
                    await run_runtime.close()
                finally:
                    await repository.close()
        finally:
            if instance_lock is not None:
                instance_lock.release()

    app = FastAPI(title="Queue Session Operator", lifespan=lifespan)
    app.mount("/static", StaticFiles(directory=_WEB_ROOT / "static"), name="static")

    @app.middleware("http")
    async def contain_failures(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        """Turn any route/template failure into a sanitized, non-destructive response.

        Scheduler, creation, and browser workers are separate tasks; a failing page
        never reaches them. Mutating routes persist through the repository's own
        transactions, so a failure while rendering the result cannot half-apply one.
        """

        try:
            return await call_next(request)
        except Exception as exc:  # noqa: BLE001 - the browser only sees a generic message
            log_event(
                logger,
                logging.ERROR,
                "web_request_failed",
                operation=f"{request.method} {request.url.path}",
                error_type=type(exc).__name__,
            )
            database = isinstance(exc, (sqlite3.Error, RepositoryError))
            reason = "database temporarily unavailable" if database else "internal error"
            return _failure_response(
                request,
                f"Request failed ({reason}). Persisted sessions are unchanged by this page; "
                "the dashboard will show current state when it next refreshes.",
            )

    def feedback(request: Request, message: str, *, status_code: int = 503) -> Response:
        return _failure_response(request, message, status_code=status_code)

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
            {
                "error": None,
                "target_url": "",
                "requested_sessions": "",
                "browser_backend": settings.browser_backend.value,
                "monitoring_strategies": tuple(MonitoringStrategy),
                "selected_monitoring_strategy": settings.monitoring_strategy.value,
                "proxy_enabled": settings.iproyal_proxy_enabled,
                "proxy_country": settings.iproyal_proxy_country,
                "proxy_lifetime": settings.iproyal_proxy_lifetime,
            },
        )

    @app.post("/setup", response_class=HTMLResponse)
    async def submit_setup(request: Request) -> Response:
        if await repository.get_active_run() is not None:
            return RedirectResponse("/dashboard", status_code=303)
        if not lifecycle["accepting"]:
            return feedback(request, _SHUTTING_DOWN)
        form = parse_qs((await request.body()).decode("utf-8"), keep_blank_values=True)
        raw_url = form.get("target_url", [""])[0].strip()
        raw_count = form.get("requested_sessions", [""])[0].strip()
        raw_strategy = form.get(
            "monitoring_strategy", [settings.monitoring_strategy.value]
        )[0].strip()
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
        try:
            monitoring_strategy = MonitoringStrategy.parse(raw_strategy)
        except ValueError:
            monitoring_strategy = settings.monitoring_strategy
            error = error or "Select a valid monitoring strategy."
        if error is not None:
            return templates.TemplateResponse(
                request,
                "setup.html",
                {
                    "error": error,
                    "target_url": raw_url,
                    "requested_sessions": raw_count,
                    "browser_backend": settings.browser_backend.value,
                    "monitoring_strategies": tuple(MonitoringStrategy),
                    "selected_monitoring_strategy": raw_strategy,
                },
                status_code=422,
            )
        if (
            settings.iproyal_proxy_enabled
            and settings.browser_backend is BrowserBackendName.CAMOUFOX
        ):
            return templates.TemplateResponse(
                request,
                "setup.html",
                {
                    "error": (
                        "IPRoyal production proxy support requires Patchright or Chrome; "
                        "Camoufox is not certified for proxied runs."
                    ),
                    "target_url": raw_url,
                    "requested_sessions": raw_count,
                    "browser_backend": settings.browser_backend.value,
                    "monitoring_strategies": tuple(MonitoringStrategy),
                    "selected_monitoring_strategy": monitoring_strategy.value,
                    "proxy_enabled": True,
                    "proxy_country": settings.iproyal_proxy_country,
                    "proxy_lifetime": settings.iproyal_proxy_lifetime,
                },
                status_code=422,
            )
        if settings.browser_backend is BrowserBackendName.CAMOUFOX:
            readiness = await camoufox_ready()
            if not readiness.passed:
                log_event(
                    logger,
                    logging.ERROR,
                    "camoufox_preflight_failed",
                    error_type=readiness.error.split(":", 1)[0] if readiness.error else None,
                )
                return templates.TemplateResponse(
                    request,
                    "setup.html",
                    {
                        "error": (
                            f"Camoufox is not ready, so no run was created. {readiness.remedy} "
                            "To use Chrome instead, set BROWSER_BACKEND=chrome and restart."
                        ),
                        "target_url": raw_url,
                        "requested_sessions": raw_count,
                        "browser_backend": settings.browser_backend.value,
                        "monitoring_strategies": tuple(MonitoringStrategy),
                        "selected_monitoring_strategy": monitoring_strategy.value,
                    },
                    status_code=503,
                )
        patchright_build: str | None = None
        if settings.browser_backend is BrowserBackendName.PATCHRIGHT:
            patchright_readiness = await patchright_ready()
            if not patchright_readiness.passed:
                log_event(
                    logger,
                    logging.ERROR,
                    "patchright_preflight_failed",
                    error_type=(
                        patchright_readiness.error.split(":", 1)[0]
                        if patchright_readiness.error
                        else None
                    ),
                )
                return templates.TemplateResponse(
                    request,
                    "setup.html",
                    {
                        "error": (
                            "Patchright is not ready, so no run was created. "
                            f"{patchright_readiness.remedy} To use standard Chrome "
                            "instead, set BROWSER_BACKEND=chrome and restart."
                        ),
                        "target_url": raw_url,
                        "requested_sessions": raw_count,
                        "browser_backend": settings.browser_backend.value,
                        "monitoring_strategies": tuple(MonitoringStrategy),
                        "selected_monitoring_strategy": monitoring_strategy.value,
                    },
                    status_code=503,
                )
            patchright_build = patchright_readiness.observed_browser_version
        run = RunConfig(
            run_id=str(uuid4()),
            target_url=target_url,
            requested_sessions=requested_sessions,
            created_at=datetime.now(UTC),
            browser_backend=settings.browser_backend,
            monitoring_strategy=monitoring_strategy,
            browser_build=(
                CAMOUFOX_BROWSER_VERSION
                if settings.browser_backend is BrowserBackendName.CAMOUFOX
                else patchright_build
            ),
            proxy_provider=(
                ProxyProvider.IPROYAL
                if settings.iproyal_proxy_enabled
                else ProxyProvider.NONE
            ),
            proxy_country=(
                settings.iproyal_proxy_country
                if settings.iproyal_proxy_enabled
                else None
            ),
            proxy_lifetime=(
                settings.iproyal_proxy_lifetime
                if settings.iproyal_proxy_enabled
                else None
            ),
        )
        try:
            await repository.create_run(run)
        except RepositoryError as exc:
            # A duplicate submission loses the singleton insert race; the first
            # one's run is the only run and the user simply continues to it.
            if await repository.get_active_run() is not None:
                return RedirectResponse("/dashboard", status_code=303)
            return templates.TemplateResponse(
                request,
                "setup.html",
                {
                    "error": str(exc),
                    "target_url": raw_url,
                    "requested_sessions": raw_count,
                    "browser_backend": settings.browser_backend.value,
                    "monitoring_strategies": tuple(MonitoringStrategy),
                    "selected_monitoring_strategy": monitoring_strategy.value,
                },
                status_code=409,
            )
        await run_runtime.start_run(run)
        return RedirectResponse("/dashboard", status_code=303)

    async def require_run() -> RunConfig | None:
        return await repository.get_active_run()

    async def sessions_context(
        *,
        page: int,
        search: str,
        status: str,
        runtime_state: str,
        action_error: str | None = None,
        action_message: str | None = None,
        partial: bool = False,
        feedback_oob: bool = False,
    ) -> dict[str, object]:
        parsed_status, parsed_runtime, filter_error = _parse_filters(status, runtime_state)
        session_page = await _session_page(
            repository, page, search, parsed_status, parsed_runtime
        )
        session_actions = {}
        for item in session_page.items:
            action = run_runtime.session_action(item.session_id)
            if action is not None:
                session_actions[item.session_id] = action
        return {
            "session_page": session_page,
            "search": search,
            "selected_status": parsed_status.value if parsed_status is not None else "",
            "selected_runtime": parsed_runtime.value if parsed_runtime is not None else "",
            "filter_error": filter_error,
            "action_error": action_error,
            "action_message": action_message,
            "session_actions": session_actions,
            "add_action": run_runtime.latest_add_action(),
            "render_token": uuid4().hex,
            "partial": partial,
            "feedback_oob": feedback_oob,
        }

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
        context = await sessions_context(
            page=page, search=search, status=status, runtime_state=runtime_state
        )
        return templates.TemplateResponse(
            request,
            "dashboard.html",
            {
                "summary": await dashboard.summary(run),
                **context,
                "statuses": tuple(QueueStatus),
                "runtime_states": tuple(BrowserRuntimeState),
            },
        )

    async def summary_response(request: Request, run: RunConfig) -> Response:
        return templates.TemplateResponse(
            request,
            "_summary.html",
            {"summary": await dashboard.summary(run), "partial": True},
        )

    @app.get("/metrics")
    async def metrics_endpoint() -> Response:
        """Aggregate Prometheus exposition for the current run (no per-session labels)."""

        exposition = getattr(run_runtime, "metrics_exposition", None)
        body = await exposition() if exposition is not None else b""
        return Response(content=body, media_type=CONTENT_TYPE_LATEST)

    @app.get("/partials/summary", response_class=HTMLResponse)
    async def summary_partial(request: Request) -> Response:
        run = await require_run()
        if run is None:
            return RedirectResponse("/setup", status_code=303)
        return await summary_response(request, run)

    @app.post("/monitoring/pause", response_class=HTMLResponse)
    async def pause_monitoring(request: Request) -> Response:
        run = await require_run()
        if run is None:
            return RedirectResponse("/setup", status_code=303)
        if not lifecycle["accepting"]:
            return feedback(request, _SHUTTING_DOWN)
        await run_runtime.pause_monitoring()
        return await summary_response(request, run)

    @app.post("/monitoring/resume", response_class=HTMLResponse)
    async def resume_monitoring(request: Request) -> Response:
        run = await require_run()
        if run is None:
            return RedirectResponse("/setup", status_code=303)
        if not lifecycle["accepting"]:
            return feedback(request, _SHUTTING_DOWN)
        await run_runtime.resume_monitoring()
        return await summary_response(request, run)

    @app.post("/run/reset", response_class=HTMLResponse)
    async def reset_run(request: Request) -> Response:
        """Stop all work and delete the run so the next load starts at setup."""

        if await require_run() is None:
            return RedirectResponse("/setup", status_code=303)
        if lifecycle["resetting"]:
            return feedback(request, "Reset already in progress.")
        if not lifecycle["accepting"]:
            return feedback(request, _SHUTTING_DOWN)
        lifecycle["resetting"] = True
        lifecycle["accepting"] = False
        try:
            await run_runtime.reset()
        except Exception as exc:  # noqa: BLE001 - reported without internal detail
            log_event(
                logger,
                logging.ERROR,
                "run_reset_failed",
                error_type=type(exc).__name__,
            )
            restored = await repository.get_active_run() is not None
            return feedback(
                request,
                "Reset failed; sessions were not deleted and the run was restarted."
                if restored
                else "Reset failed after the run was deleted; reload to continue at setup.",
            )
        finally:
            lifecycle["accepting"] = not lifecycle["stopping"]
            lifecycle["resetting"] = False
        if request.headers.get("HX-Request") == "true":
            return Response(status_code=204, headers={"HX-Redirect": "/setup"})
        return RedirectResponse("/setup", status_code=303)

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
            await sessions_context(
                page=page,
                search=search,
                status=status,
                runtime_state=runtime_state,
                partial=True,
            ),
        )

    async def mutation_response(
        request: Request,
        *,
        page: int,
        search: str,
        status: str,
        runtime_state: str,
        error: str | None,
        message: str | None,
    ) -> Response:
        return templates.TemplateResponse(
            request,
            "_sessions.html",
            await sessions_context(
                page=page,
                search=search,
                status=status,
                runtime_state=runtime_state,
                action_error=error,
                action_message=message,
                partial=True,
                feedback_oob=True,
            ),
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
        if not lifecycle["accepting"]:
            return feedback(request, _SHUTTING_DOWN)
        message: str | None = None
        error: str | None = None
        try:
            message = (await run_runtime.open_session(session_id)).message
        except (ManualOpenError, RepositoryError) as exc:
            error = str(exc)
        return await mutation_response(
            request,
            page=page,
            search=search,
            status=status,
            runtime_state=runtime_state,
            error=error,
            message=message,
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
        # Close stays available during shutdown: it only releases resources.
        closed = await run_runtime.close_session(session_id)
        return await mutation_response(
            request,
            page=page,
            search=search,
            status=status,
            runtime_state=runtime_state,
            error=None if closed else "Session is not open in a browser",
            message="Browser session closed" if closed else None,
        )

    async def submit_operator_action(
        request: Request,
        kind: OperatorActionKind,
        session_id: str | None,
        page: int,
        search: str,
        status: str,
        runtime_state: str,
        token: str,
    ) -> Response:
        if await require_run() is None:
            return RedirectResponse("/setup", status_code=303)
        if not lifecycle["accepting"]:
            return feedback(request, _SHUTTING_DOWN)
        request_token = f"{token}:{kind.value}:{session_id or ''}" if token else None
        action = await run_runtime.request_action(
            kind, session_id, request_token=request_token
        )
        error = action.message if action.status is OperatorActionStatus.FAILED else None
        message = None if error is not None else action.message
        return await mutation_response(
            request,
            page=page,
            search=search,
            status=status,
            runtime_state=runtime_state,
            error=error,
            message=message,
        )

    def action_route(kind: OperatorActionKind):  # type: ignore[no-untyped-def]
        async def route(
            request: Request,
            session_id: str,
            page: int = Query(1, ge=1),
            search: str = "",
            status: str = "",
            runtime_state: str = "",
            token: str = "",
        ) -> Response:
            return await submit_operator_action(
                request, kind, session_id, page, search, status, runtime_state, token
            )

        return route

    app.post("/sessions/{session_id}/refresh", response_class=HTMLResponse)(
        action_route(OperatorActionKind.REFRESH)
    )
    app.post("/sessions/{session_id}/delete", response_class=HTMLResponse)(
        action_route(OperatorActionKind.DELETE)
    )
    app.post("/sessions/{session_id}/replace", response_class=HTMLResponse)(
        action_route(OperatorActionKind.REPLACE)
    )

    @app.post("/sessions/new", response_class=HTMLResponse)
    async def add_session(
        request: Request,
        page: int = Query(1, ge=1),
        search: str = "",
        status: str = "",
        runtime_state: str = "",
        token: str = "",
    ) -> Response:
        return await submit_operator_action(
            request,
            OperatorActionKind.ADD,
            None,
            page,
            search,
            status,
            runtime_state,
            token,
        )

    return app


def _failure_response(request: Request, message: str, *, status_code: int = 503) -> Response:
    """A static fragment: rendering it touches neither templates nor the database."""

    safe = escape(message)
    if request.headers.get("HX-Request") != "true":
        return HTMLResponse(
            "<!doctype html><title>Queue Session Operator</title>"
            f'<p role="alert">{safe}</p><p><a href="/dashboard">Back to dashboard</a></p>',
            status_code=status_code,
        )
    target = "#refresh-status" if request.method == "GET" else "#action-feedback"
    return HTMLResponse(
        f'<p class="error" role="alert">{safe}</p>',
        status_code=status_code,
        headers={"HX-Retarget": target, "HX-Reswap": "innerHTML"},
    )


def local_time_markup(value: datetime | None) -> Markup:
    """A ``<time>`` element the dashboard script shows in the viewer's local timezone.

    The attribute carries the exact UTC instant; the text is a readable UTC fallback
    for a browser without JavaScript.
    """

    if value is None:
        return Markup("—")
    instant = (value if value.tzinfo is not None else value.replace(tzinfo=UTC)).astimezone(UTC)
    return Markup(
        '<time class="local-time" datetime="{iso}">{fallback}</time>'
    ).format(
        iso=instant.isoformat().replace("+00:00", "Z"),
        fallback=instant.strftime("%d %b %H:%M:%S UTC"),
    )


def _parse_filters(
    status: str,
    runtime_state: str,
) -> tuple[QueueStatus | None, BrowserRuntimeState | None, str | None]:
    parsed_status: QueueStatus | None = None
    parsed_runtime: BrowserRuntimeState | None = None
    errors: list[str] = []
    if status:
        try:
            parsed_status = QueueStatus.parse(status)
        except ValueError:
            errors.append("lifecycle status")
    if runtime_state:
        try:
            parsed_runtime = BrowserRuntimeState(runtime_state)
        except ValueError:
            errors.append("browser state")
    message = f"Ignored unknown {' and '.join(errors)} filter." if errors else None
    return parsed_status, parsed_runtime, message


async def _session_page(
    repository: SessionRepository,
    page: int,
    search: str,
    status: QueueStatus | None,
    runtime_state: BrowserRuntimeState | None,
) -> SessionSummaryPage:
    result = await repository.list_session_summaries(
        page=page,
        page_size=_PAGE_SIZE,
        search=search or None,
        status=status,
        runtime_state=runtime_state,
    )
    if result.items or page <= result.page_count:
        return result
    # A deleted row can shrink the population under a deep page; show the last one.
    return await repository.list_session_summaries(
        page=result.page_count,
        page_size=_PAGE_SIZE,
        search=search or None,
        status=status,
        runtime_state=runtime_state,
    )
