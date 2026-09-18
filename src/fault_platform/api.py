"""Local web server hosting the designer and shared pipeline control API.

路由分三类：

* **页面与静态资源**：``/`` 返回设计器页面，``/static`` 提供前端文件；
* **控制 API**：``POST /api/control/{operation}`` 转发到
  :meth:`fault_platform.service.PipelineService.dispatch`，
  ``/api/health`` 与 ``/api/data``、``/api/data/upload`` 提供健康检查与数据文件管理；
* **文件接口**：``GET /api/node-data`` 列出某节点可导出的中间产物，
  ``GET /api/node-data/csv`` 把它作为 CSV 附件下载（给人在本机核对，不是控制动作）；
* **事件流**：``GET /api/events`` 是 SSE，把图修订、节点状态与运行状态实时推给页面。

安全边界：服务只监听回环地址，并额外做了三层防护——
``TrustedHostMiddleware`` 限制 Host、中间件校验同源（跨源写操作直接 403）、
响应头带 nosniff 与 CSP。这是面向本机单用户的开发服务，不含鉴权与多租户隔离。
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import quote
from uuid import uuid4

import pandas as pd
from fastapi import FastAPI, File, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from fault_platform.events import HEARTBEAT_SECONDS
from fault_platform.service import DEFAULT_EXPORT_ROWS, PipelineService
from fault_platform.version import PLATFORM_VERSION
from fault_platform.workspace import json_safe

WEB_ROOT = Path(__file__).with_name("web")


def create_app(
    data_root: str | Path = "examples/data",
    storage_root: str | Path | None = None,
    service: PipelineService | None = None,
    artifact_cache_mb: int | None = None,
    artifact_spill_dir: str | Path | None = None,
) -> FastAPI:
    """构造 FastAPI 应用；``service`` 可注入（测试里传入自建的服务实例）。"""
    control = service or PipelineService(
        data_root, storage_root, artifact_cache_mb=artifact_cache_mb, artifact_spill_dir=artifact_spill_dir
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        """应用生命周期：关闭时释放服务（停线程池、释放产物与溢写文件）。"""
        yield
        control.close()

    app = FastAPI(title="Fault Prediction Component Platform", version=PLATFORM_VERSION, lifespan=lifespan)
    app.state.service = control
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "[::1]", "testserver"])

    @app.middleware("http")
    async def local_origin(request: Request, call_next):
        """只允许同源的写操作，并统一追加安全响应头。

        读操作（GET/HEAD/OPTIONS）不检查来源；写操作若带 ``Origin`` 头，
        则它的主机必须与请求的 Host 一致——这样其它网页无法通过浏览器悄悄改本机的方案。
        """
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
        """健康检查：状态、版本、组件数与缓存统计（部署验收脚本用它）。"""
        return {
            "status": "ok",
            "version": PLATFORM_VERSION,
            "components": len(control.registry),
            "artifact_cache": control.workspaces.cache_stats(),
        }

    @app.post("/api/control/{operation}")
    def dispatch(operation: str, arguments: dict[str, Any]):
        """控制 API 的统一出口：业务失败返回 400 且带结构化错误，成功返回 200。"""
        result = control.dispatch(operation, arguments)
        return JSONResponse(result, status_code=200 if result.get("success") else 400)

    @app.get("/api/events")
    async def events(request: Request, pipeline_id: str | None = None, last_event_id: str | None = None):
        """SSE 事件流：图修订、节点状态迁移与运行状态。

        连接建立时先补发 ``last_event_id`` 之后的历史事件（断线重连不丢帧），
        之后每 ``HEARTBEAT_SECONDS`` 发一次注释行保活；客户端断开时注销订阅。
        """
        try:
            subscription, replay = control.subscribe_events(pipeline_id, last_event_id)
        except ValueError as exc:
            return JSONResponse({"success": False, "summary": str(exc)}, status_code=404)

        async def stream():
            """实际的生成器：先发一行握手注释，再补历史帧，最后持续推送新帧。"""
            try:
                yield ": connected\n\n"
                for event in replay:
                    yield event.to_sse(control.bus.frame_id(event))
                while True:
                    # 阻塞式取队列放到线程里：不阻塞事件循环，其它 HTTP 请求照常处理。
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
        """列出数据目录下的 CSV/Parquet（最多 200 条），供页面选择输入文件。

        再次校验路径在 ``data_root`` 内：``glob`` 拿到的是路径对象，
        这里做一次归一化确认，避免符号链接把目录外的文件暴露出去。
        """
        patterns = ("**/*.csv", "**/*.parquet", "**/*.pq")
        paths = sorted(path for pattern in patterns for path in control.data_root.glob(pattern))[:200]
        return {
            "datasets": [
                {"path": p.relative_to(control.data_root).as_posix(), "bytes": p.stat().st_size}
                for p in paths
                if p.resolve().is_relative_to(control.data_root)
            ]
        }

    @app.get("/api/node-data")
    def node_data(pipeline_id: str, node_id: str):
        """列出该节点可导出的表格产物（输入侧来自上游端口，输出侧来自本节点）。

        这是给"点节点 → 导出"用的文件级接口，刻意**不**进控制操作表：它服务的是人来看，
        不是一个可编程控制动作，因此也不会变成第 40 个 MCP 工具。
        """
        try:
            return control.list_node_data(pipeline_id, node_id)
        except (ValueError, KeyError) as exc:
            return JSONResponse({"success": False, "summary": str(exc)}, status_code=400)

    @app.get("/api/node-data/csv")
    def node_data_csv(
        pipeline_id: str,
        node_id: str,
        direction: str = "output",
        port: str | None = None,
        max_rows: int = DEFAULT_EXPORT_ROWS,
    ):
        """把某节点某一侧的表格产物作为 CSV 附件下载（``max_rows=0`` 表示不截断）。

        响应头里带 ``X-Rows`` / ``X-Total-Rows`` / ``X-Truncated``：截断了就在文件里截断，
        但绝不让下载方以为拿到的是全量。
        """
        try:
            payload = control.export_node_data(
                pipeline_id, node_id, direction=direction, port=port, max_rows=max_rows
            )
        except (ValueError, KeyError) as exc:
            return JSONResponse({"success": False, "summary": str(exc)}, status_code=400)
        filename = payload["filename"]
        return Response(
            content=payload["csv"],
            media_type="text/csv; charset=utf-8",
            headers={
                # 两种写法都给：老客户端读 filename，支持 RFC 5987 的读 filename*。
                "Content-Disposition": (
                    f"attachment; filename=\"{filename}\"; filename*=UTF-8''{quote(filename)}"
                ),
                "X-Rows": str(payload["rows"]),
                "X-Total-Rows": str(payload["total_rows"]),
                "X-Truncated": "true" if payload["truncated"] else "false",
            },
        )

    @app.post("/api/data/upload")
    async def upload(file: UploadFile = File(...)):
        """上传数据文件：只接受 CSV/Parquet，单个文件上限 25 MB。

        文件名会被替换成 ``dataset_<随机>.<后缀>``，避免路径穿越与重名覆盖；
        写入后立刻尝试解析前 5 行，解析不了就删除文件并报错。
        更大的文件请直接放进 ``data_root``（或用 ``--data-root`` 指向数据目录）。
        """
        suffix = Path(file.filename or "dataset.csv").suffix.lower()
        if suffix not in {".csv", ".parquet", ".pq"}:
            return JSONResponse(
                {"success": False, "summary": "Upload CSV or Parquet (.csv, .parquet, .pq)"},
                status_code=400,
            )
        destination = control.data_root / "uploads" / f"dataset_{uuid4().hex[:12]}{suffix}"
        destination.parent.mkdir(parents=True, exist_ok=True)
        size = 0
        try:
            with destination.open("wb") as handle:
                while chunk := await file.read(1024 * 1024):
                    size += len(chunk)
                    if size > 25 * 1024 * 1024:
                        # 边写边计数，超限时立即中止，不必等文件传完。
                        raise ValueError("Upload limit is 25 MB; place larger files in the data directory")
                    handle.write(chunk)
            if suffix == ".csv":
                preview = pd.read_csv(destination, nrows=5, encoding="utf-8-sig")
            else:
                preview = pd.read_parquet(destination).head(5)
            if preview.empty:
                raise ValueError("Uploaded file has no data rows")
            return {
                "success": True,
                "path": destination.relative_to(control.data_root).as_posix(),
                "columns": list(preview.columns),
                "preview": json_safe(preview.to_dict(orient="records")),
            }
        except Exception as exc:
            # 失败时清理半成品，避免留下一个"看起来存在但读不了"的文件。
            destination.unlink(missing_ok=True)
            return JSONResponse({"success": False, "summary": str(exc)}, status_code=400)
        finally:
            await file.close()

    app.mount("/static", StaticFiles(directory=WEB_ROOT), name="static")
    return app
