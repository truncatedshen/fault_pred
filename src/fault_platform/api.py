"""Local web server hosting the designer and shared pipeline control API."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from uuid import uuid4

import pandas as pd
from fastapi import FastAPI, File, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from fault_platform.events import HEARTBEAT_SECONDS
from fault_platform.service import PipelineService
from fault_platform.workspace import json_safe

WEB_ROOT = Path(__file__).with_name("web")


def create_app(
    data_root: str | Path = "examples/data",
    storage_root: str | Path | None = None,
    service: PipelineService | None = None,
    artifact_cache_mb: int | None = None,
    artifact_spill_dir: str | Path | None = None,
) -> FastAPI:
    control = service or PipelineService(
        data_root, storage_root, artifact_cache_mb=artifact_cache_mb, artifact_spill_dir=artifact_spill_dir
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        yield
        control.close()

    app = FastAPI(title="Fault Prediction Component Platform", version="0.1.0", lifespan=lifespan)
    app.state.service = control
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "[::1]", "testserver"])

    @app.middleware("http")
    async def local_origin(request: Request, call_next):
        origin = request.headers.get("origin")
        if request.method not in {"GET", "HEAD", "OPTIONS"} and origin:
            from urllib.parse import urlparse

            parsed = urlparse(origin)
            if parsed.netloc != request.headers.get("host") or parsed.scheme not in {"http", "https"}:
                return JSONResponse(
                    {"success": False, "summary": "Cross-origin mutation is not allowed"}, status_code=403
                )
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'"
        )
        return response

    @app.get("/")
    def index():
        return FileResponse(WEB_ROOT / "index.html")

    @app.get("/api/health")
    def health():
        return {
            "status": "ok",
            "version": "0.1.0",
            "components": len(control.registry.list()),
            "artifact_cache": control.workspaces.cache_stats(),
        }

    @app.post("/api/control/{operation}")
    def dispatch(operation: str, arguments: dict[str, Any]):
        result = control.dispatch(operation, arguments)
        return JSONResponse(result, status_code=200 if result.get("success") else 400)

    @app.get("/api/events")
    async def events(request: Request, pipeline_id: str | None = None, last_event_id: str | None = None):
        """Server-Sent Events: graph revisions, node transitions and run status."""
        try:
            subscription, replay = control.subscribe_events(pipeline_id, last_event_id)
        except ValueError as exc:
            return JSONResponse({"success": False, "summary": str(exc)}, status_code=404)

        async def stream():
            try:
                yield ": connected\n\n"
                for event in replay:
                    yield event.to_sse(control.bus.frame_id(event))
                while True:
                    event = await asyncio.to_thread(subscription.get, HEARTBEAT_SECONDS)
                    if event is None:
                        if subscription.closed or await request.is_disconnected():
                            break
                        yield ": keep-alive\n\n"
                        continue
                    yield event.to_sse(control.bus.frame_id(event))
            finally:
                control.unsubscribe_events(subscription)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
        )

    @app.get("/api/data")
    def datasets():
        paths = sorted(control.data_root.glob("**/*.csv"))[:200]
        return {
            "datasets": [
                {"path": p.relative_to(control.data_root).as_posix(), "bytes": p.stat().st_size}
                for p in paths
                if p.resolve().is_relative_to(control.data_root)
            ]
        }

    @app.post("/api/data/upload")
    async def upload(file: UploadFile = File(...)):
        destination = control.data_root / "uploads" / f"dataset_{uuid4().hex[:12]}.csv"
        destination.parent.mkdir(parents=True, exist_ok=True)
        size = 0
        try:
            with destination.open("wb") as handle:
                while chunk := await file.read(1024 * 1024):
                    size += len(chunk)
                    if size > 25 * 1024 * 1024:
                        raise ValueError(
                            "CSV upload limit is 25 MB; place larger files in the data directory"
                        )
                    handle.write(chunk)
            preview = pd.read_csv(destination, nrows=5, encoding="utf-8-sig")
            if preview.empty:
                raise ValueError("CSV has no data rows")
            return {
                "success": True,
                "path": destination.relative_to(control.data_root).as_posix(),
                "columns": list(preview.columns),
                "preview": json_safe(preview.to_dict(orient="records")),
            }
        except Exception as exc:
            destination.unlink(missing_ok=True)
            return JSONResponse({"success": False, "summary": str(exc)}, status_code=400)
        finally:
            await file.close()

    app.mount("/static", StaticFiles(directory=WEB_ROOT), name="static")
    return app
