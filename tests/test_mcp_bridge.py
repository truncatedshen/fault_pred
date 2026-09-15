"""MCP 桥自身的测试。

服务层的测试覆盖不到传输层：``wait_for_pipeline`` 曾经固定 60 秒客户端超时，于是在真实数据上
跑两分钟的任务会被返回成 ``CONTROL_API_UNAVAILABLE``（"启动服务"），而服务端其实还在 RUNNING。
Agent 若相信这句话去重启服务，内存里的图与产物全部丢失。
这两条不变量（描述齐全、超时按工具区分）现在有测试守着。
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest

mcp_server = pytest.importorskip("fault_platform.mcp_server")

from fault_platform.service import CONTROL_OPERATIONS  # noqa: E402


class _FakeClient:
    """假的 httpx.AsyncClient：默认抛读超时，子类可以改成返回某个响应。"""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.kwargs = kwargs

    async def __aenter__(self) -> "_FakeClient":
        return self

    async def __aexit__(self, *exc: Any) -> bool:
        return False

    async def post(self, *args: Any, **kwargs: Any) -> httpx.Response:
        raise httpx.ReadTimeout("read timeout", request=httpx.Request("POST", "http://127.0.0.1:8765"))


class _JsonClient(_FakeClient):
    """返回一个正常的 JSON 控制响应。"""

    async def post(self, *args: Any, **kwargs: Any) -> httpx.Response:
        return httpx.Response(
            200,
            json={"success": True, "status": "SUCCESS"},
            request=httpx.Request("POST", "http://127.0.0.1:8765"),
        )


class _HtmlClient(_FakeClient):
    """返回非 JSON 正文（例如反代吐出来的错误页）。"""

    async def post(self, *args: Any, **kwargs: Any) -> httpx.Response:
        return httpx.Response(
            500, text="<html>boom</html>", request=httpx.Request("POST", "http://127.0.0.1:8765")
        )


def test_every_control_operation_has_a_written_description() -> None:
    """没有描述的操作只会显示一句兜底文案（"Wait For Pipeline."），等于让 Agent 靠猜。"""
    missing = sorted(name for name in CONTROL_OPERATIONS if name not in mcp_server.DESCRIPTIONS)
    assert not missing, f"operations without a description: {missing}"
    thin = sorted(name for name, text in mcp_server.DESCRIPTIONS.items() if len(text) < 40)
    assert not thin, f"descriptions too thin to be useful: {thin}"


def test_wait_timeout_follows_the_requested_timeout() -> None:
    """等待类工具的客户端超时必须跟着 timeout_seconds 走，否则长跑会被误判成服务不可达。"""
    assert mcp_server.call_timeout_seconds("execute_pipeline", {}) == 60.0
    assert mcp_server.call_timeout_seconds("wait_for_pipeline", {}) > 300.0
    assert mcp_server.call_timeout_seconds("wait_for_pipeline", {"timeout_seconds": 600}) > 600.0
    # 非法值退回默认，而不是在桥里抛异常：真正非法时服务端会报参数错误。
    assert mcp_server.call_timeout_seconds("wait_for_pipeline", {"timeout_seconds": "soon"}) > 300.0


def test_transport_timeout_is_not_reported_as_a_dead_service(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mcp_server.httpx, "AsyncClient", _FakeClient)
    result = asyncio.run(
        mcp_server._proxy_for("wait_for_pipeline", "http://127.0.0.1:8765")(
            pipeline_id="pipeline_x", timeout_seconds=300
        )
    )
    assert result["success"] is False
    assert result["error_code"] == "CONTROL_API_TIMEOUT"
    assert "do not restart" in result["recommended_action"]
    assert "get_pipeline_status" in result["recommended_action"]


def test_non_json_reply_is_reported_as_a_platform_bug(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mcp_server.httpx, "AsyncClient", _HtmlClient)
    result = asyncio.run(mcp_server._proxy_for("get_server_info", "http://127.0.0.1:8765")())
    assert result["error_code"] == "CONTROL_API_BAD_RESPONSE"
    assert "platform bug" in result["recommended_action"]


def test_successful_reply_is_passed_through(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mcp_server.httpx, "AsyncClient", _JsonClient)
    result = asyncio.run(mcp_server._proxy_for("get_server_info", "http://127.0.0.1:8765")())
    assert result == {"success": True, "status": "SUCCESS"}
