import os
from pathlib import Path
from fastapi import FastAPI, Request, HTTPException, status
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv

from dashboard.backend.routes_pipeline import router as pipeline_router
from dashboard.backend.routes_agent import router as agent_router
from dashboard.backend.routes_schedule import router as schedule_router
from dashboard.backend.routes_youtube import router as youtube_router
from dashboard.backend.routes_rights import router as rights_router
from dashboard.backend.db import WORKSPACE_DIR
from dashboard.backend.security import (
    is_host_allowed,
    verify_request_host,
    verify_api_token,
    verify_media_access,
    generate_media_token,
)

_env_path = WORKSPACE_DIR / ".env"
if _env_path.is_file():
    load_dotenv(dotenv_path=_env_path)
else:
    load_dotenv()

FRONTEND_DIST = WORKSPACE_DIR / "dashboard" / "frontend" / "dist"


def create_app() -> FastAPI:
    app = FastAPI(
        title="100B Local Pipeline Dashboard",
        docs_url=None,  # Disabled for security
        redoc_url=None,
    )

    # Security & Auth Middleware
    @app.middleware("http")
    async def security_and_auth_middleware(request: Request, call_next):
        # 1. Host check (OPTIONS skips preflight)
        if request.method != "OPTIONS":
            try:
                verify_request_host(request)
            except HTTPException as he:
                return JSONResponse(
                    status_code=he.status_code,
                    content={"error": he.detail},
                )

        # 2. API Authentication check for /api/ routes
        # Skip health check, OPTIONS preflights, and static assets
        path = request.url.path
        if path.startswith("/api/") and path != "/api/health" and request.method != "OPTIONS":
            # Media endpoints (/api/thumbs, /api/video) use signed media tokens or API key
            if path.startswith("/api/thumbs/") or path.startswith("/api/video/"):
                try:
                    verify_media_access(request)
                except HTTPException as he:
                    return JSONResponse(status_code=he.status_code, content={"error": he.detail})
            else:
                # Regular API routes require X-Dashboard-Token (if DASHBOARD_API_KEY is configured)
                try:
                    verify_api_token(request)
                except HTTPException as he:
                    return JSONResponse(status_code=he.status_code, content={"error": he.detail})

        response = await call_next(request)
        # Security headers
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "SAMEORIGIN"
        return response

    # Include API Routers
    app.include_router(pipeline_router)
    app.include_router(agent_router)
    app.include_router(schedule_router)
    app.include_router(youtube_router)
    app.include_router(rights_router)

    @app.get("/api/health")
    def health_check():
        return {"status": "ok", "app": "100B Dashboard"}

    @app.get("/api/auth/verify")
    def auth_verify(request: Request):
        verify_api_token(request)
        return {"status": "ok", "message": "Authentication successful"}

    @app.get("/api/auth/media-token")
    def media_token(request: Request):
        verify_api_token(request)
        return generate_media_token(validity_seconds=600)

    # Mount static assets if frontend has been built
    assets_dir = FRONTEND_DIST / "assets"
    if assets_dir.is_dir():
        app.mount("/assets", StaticFiles(directory=str(assets_dir)), name="assets")

    # Client-side SPA fallback for all frontend views
    @app.get("/{full_path:path}")
    async def serve_spa(full_path: str):
        index_file = FRONTEND_DIST / "index.html"
        if index_file.is_file():
            # Check if specific static file exists in dist
            target = FRONTEND_DIST / full_path
            if target.is_file() and not full_path.endswith(".html"):
                return FileResponse(target)
            return FileResponse(index_file)
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content={"message": "100B Dashboard Backend Active. Build frontend into dashboard/frontend/dist to view UI."},
        )

    # Configure CORS - added LAST so it is the outermost middleware.
    # This guarantees that 401/403 responses and OPTIONS preflights receive proper CORS headers.
    raw_cors = os.getenv("DASHBOARD_CORS_ORIGINS", "")
    allowed_origins = [o.strip() for o in raw_cors.split(",") if o.strip()]
    if not allowed_origins:
        allowed_origins = [
            "http://localhost:5173",
            "http://127.0.0.1:5173",
            "http://localhost:8765",
            "http://127.0.0.1:8765",
        ]

    app.add_middleware(
        CORSMiddleware,
        allow_origins=allowed_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
        allow_headers=["*"],
        expose_headers=["Content-Range", "Accept-Ranges", "Content-Length"],
    )

    return app


app = create_app()
