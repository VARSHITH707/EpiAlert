"""EpiAlert web application.

FastAPI with server-rendered Jinja2 templates. No JavaScript build step: the
pages are plain HTML so the app runs from a clean checkout with nothing but
the Python dependencies.

Start it with:
    .venv\\Scripts\\python.exe -m uvicorn src.web.main:app --reload

Create the first user first:
    .venv\\Scripts\\python.exe -m src.cli createuser --username admin

Routes are thin. Queries live in src.web.data, authentication in src.web.auth.
Every route other than /login and /health depends on require_user, so the
server rejects unauthenticated requests rather than relying on the UI to hide
them.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from fastapi import (
    Depends, FastAPI, File, Form, HTTPException, Request, UploadFile, status,
)
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from src.web import data
from src.web.upload import UploadError, next_available_week, process_uploaded_week
from src.web.auth import (
    SESSION_COOKIE,
    authenticate,
    current_user,
    make_session,
    require_user,
    user_count,
)

logger = logging.getLogger("web")

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def create_app() -> FastAPI:
    """Build the FastAPI application."""
    app = FastAPI(title="EpiAlert", version="1.0.0")
    _register_error_handlers(app)
    _register_auth_routes(app)
    _register_page_routes(app)
    _register_api_routes(app)
    return app


def _wants_html(request: Request) -> bool:
    """True when this looks like a browser asking for a page.

    An /api/ path is always treated as programmatic, whatever it claims to
    accept, so a script gets a status code rather than a redirect it might
    silently follow into a login page and then parse as data.
    """
    if request.url.path.startswith("/api/"):
        return False
    return "text/html" in request.headers.get("accept", "")


def _register_error_handlers(app: FastAPI) -> None:
    """Send signed-out browsers to the login page instead of raw JSON.

    The 401 itself is correct -- the server is refusing an unauthenticated
    request either way. This only changes how the refusal is presented: a
    person typing an address gets the login form, while an API client still
    gets 401 and can act on it.
    """

    @app.exception_handler(HTTPException)
    async def http_exception_handler(request: Request, exc: HTTPException):
        if exc.status_code == status.HTTP_401_UNAUTHORIZED and _wants_html(request):
            return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": exc.detail},
            headers=getattr(exc, "headers", None),
        )


def _page(request: Request, template: str, user: str, **context) -> HTMLResponse:
    """Render a template with the shared page context."""
    return TEMPLATES.TemplateResponse(
        request, template, {"user": user, **context}
    )


def _register_auth_routes(app: FastAPI) -> None:
    """Public routes: health, login, logout."""

    # -- health, unauthenticated by design -------------------------------
    @app.get("/health")
    def health() -> dict:
        """Liveness probe. Carries no data, so it needs no authentication."""
        return {"status": "ok"}

    # -- login / logout ---------------------------------------------------
    @app.get("/login", response_class=HTMLResponse)
    def login_form(request: Request) -> HTMLResponse:
        if current_user(request):
            return RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)
        return TEMPLATES.TemplateResponse(
            request,
            "login.html",
            {"error": None, "no_users": user_count() == 0},
        )

    @app.post("/login")
    def login_submit(
        request: Request,
        username: str = Form(...),
        password: str = Form(...),
    ):
        if authenticate(username, password) is None:
            logger.info("Failed login for %r", username)
            # Deliberately vague: naming which half was wrong would let an
            # attacker enumerate valid usernames.
            return TEMPLATES.TemplateResponse(
                request,
                "login.html",
                {"error": "Invalid username or password", "no_users": False},
                status_code=status.HTTP_401_UNAUTHORIZED,
            )
        response = RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)
        response.set_cookie(
            SESSION_COOKIE,
            make_session(username),
            httponly=True,   # not readable from JavaScript
            samesite="lax",  # not sent on cross-site POSTs
        )
        logger.info("User %r signed in", username)
        return response

    @app.get("/logout")
    def logout() -> RedirectResponse:
        response = RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
        response.delete_cookie(SESSION_COOKIE)
        return response


def _register_page_routes(app: FastAPI) -> None:
    """HTML pages. Every one requires a signed-in user."""

    @app.get("/", response_class=HTMLResponse)
    def dashboard(request: Request, user: str = Depends(require_user)):
        return _page(
            request, "dashboard.html", user,
            counts=data.dashboard_counts(),
            recent=data.recent_alerts(limit=10),
            breakdown=data.status_breakdown(),
        )

    @app.get("/results", response_class=HTMLResponse)
    def results(
        request: Request,
        user: str = Depends(require_user),
        disease: Optional[str] = None,
        village: Optional[str] = None,
        street: Optional[str] = None,
        week: Optional[int] = None,
        status_filter: Optional[str] = None,
        fusion_mode: str = "confirmation",
    ):
        return _page(
            request, "results.html", user,
            rows=data.detection_results(
                disease=disease, village=village, street=street,
                week=week, status=status_filter, fusion_mode=fusion_mode,
            ),
            options=data.filter_options(),
            selected={
                "disease": disease, "village": village, "street": street,
                "week": week, "status": status_filter,
                "fusion_mode": fusion_mode,
            },
        )

    @app.get("/alerts", response_class=HTMLResponse)
    def alerts_page(
        request: Request,
        user: str = Depends(require_user),
        status_filter: Optional[str] = None,
    ):
        return _page(
            request, "alerts.html", user,
            rows=data.alerts(status=status_filter),
            selected_status=status_filter,
        )

    @app.get("/alerts/{alert_id}", response_class=HTMLResponse)
    def alert_detail(
        request: Request,
        alert_id: int,
        user: str = Depends(require_user),
    ):
        alert = data.alert_detail(alert_id)
        if alert is None:
            raise HTTPException(status_code=404, detail="Alert not found")
        return _page(request, "alert_detail.html", user, alert=alert)

    @app.get("/statistics", response_class=HTMLResponse)
    def statistics(request: Request, user: str = Depends(require_user)):
        return _page(
            request, "statistics.html", user,
            breakdown=data.status_breakdown(),
            counts=data.dashboard_counts(),
        )

    @app.get("/evaluation", response_class=HTMLResponse)
    def evaluation(request: Request, user: str = Depends(require_user)):
        return _page(
            request, "evaluation.html", user,
            comparison=data.evaluation_comparison(),
        )

    @app.get("/sms", response_class=HTMLResponse)
    def sms_page(request: Request, user: str = Depends(require_user)):
        return _page(
            request, "sms.html", user,
            rows=data.sms_messages(),
            summary=data.sms_summary(),
        )

    @app.get("/upload", response_class=HTMLResponse)
    def upload_form(request: Request, user: str = Depends(require_user)):
        return _page(
            request, "upload.html", user,
            next_week=next_available_week(), result=None, error=None,
        )

    @app.post("/upload", response_class=HTMLResponse)
    async def upload_submit(
        request: Request,
        user: str = Depends(require_user),
        week: int = Form(...),
        file: UploadFile = File(...),
    ):
        """Ingest an uploaded week and run detection on it.

        UploadError covers anything the user can fix by choosing a different
        file, so it is shown as a message rather than a 500.
        """
        error = None
        result = None
        try:
            result = process_uploaded_week(week, await file.read())
        except UploadError as exc:
            error = str(exc)
        except Exception as exc:  # noqa: BLE001 - reported, never swallowed
            logger.exception("Upload of week %s failed", week)
            error = f"Processing failed: {exc}"

        return _page(
            request, "upload.html", user,
            next_week=next_available_week(), result=result, error=error,
        )


def _register_api_routes(app: FastAPI) -> None:
    """JSON endpoints, protected exactly like the pages."""

    @app.get("/api/counts")
    def api_counts(user: str = Depends(require_user)) -> dict:
        return data.dashboard_counts()

    @app.get("/api/alerts")
    def api_alerts(user: str = Depends(require_user)) -> list[dict]:
        return data.alerts()

    @app.get("/api/sms-summary")
    def api_sms_summary(user: str = Depends(require_user)) -> dict:
        return data.sms_summary()


app = create_app()
