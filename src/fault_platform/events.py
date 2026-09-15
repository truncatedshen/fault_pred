"""In-process publish/subscribe bridge between the runtime and the designer.

The platform is a single local process, so a bounded in-memory bus is enough:
the execution engine publishes state transitions, the HTTP layer forwards them
to web pages as Server-Sent Events. Slow readers never block the runtime and
late subscribers can replay the recent past.

中文说明：进程内的发布/订阅总线。执行引擎发布状态变化（节点状态、图变更、历史），
HTTP 层以 SSE（``GET /api/events``）转发给浏览器，让"Agent 正在建图/执行"实时可见。

三条设计约束：

* **不阻塞执行线程**：每个订阅者一个容量 200 的队列，写满时丢最旧的帧；
* **晚来的订阅者能补帧**：总线保留最近 200 条事件，重连时按 ``Last-Event-ID`` 回放；
* **重启不串号**：事件 id 前面带 ``boot_id``，服务重启后浏览器不会把新旧 id 混在一起。
"""

from __future__ import annotations

import json
import queue
from collections import deque
from dataclasses import dataclass
from itertools import count
from threading import Lock
from typing import Any
from uuid import uuid4

MAX_QUEUE = 200
REPLAY = 200
HEARTBEAT_SECONDS = 15.0


@dataclass(frozen=True)
class PipelineEvent:
    """一条事件：自增 id、所属方案、类型（``node_status``/``pipeline_status``/…）与数据。"""

    event_id: int
    pipeline_id: str
    type: str
    data: dict[str, Any]

    def to_sse(self, frame_id: str | None = None) -> str:
        """渲染成一个 SSE 帧；事件类型放在 JSON 体内，``id:`` 用带 boot_id 的帧号。"""
        payload = {"type": self.type, "pipeline_id": self.pipeline_id, "event_id": self.event_id, **self.data}
        identifier = frame_id or str(self.event_id)
        return f"id: {identifier}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


class Subscription:
    """一个读者（通常是一个浏览器连接），带容量受限的积压队列。"""

    def __init__(self, pipeline_id: str | None) -> None:
        self.pipeline_id = pipeline_id
        self.queue: queue.Queue[PipelineEvent | None] = queue.Queue(maxsize=MAX_QUEUE)
        self.closed = False

    def offer(self, event: PipelineEvent) -> None:
        """投递事件；队列满时丢弃最旧的一条，保证执行线程永远不被慢客户端拖住。"""
        if self.closed:
            return
        try:
            self.queue.put_nowait(event)
        except queue.Full:
            # Drop the oldest frame instead of blocking the executing thread.
            try:
                self.queue.get_nowait()
            except queue.Empty:
                pass
            try:
                self.queue.put_nowait(event)
            except queue.Full:
                pass

    def get(self, timeout: float = HEARTBEAT_SECONDS) -> PipelineEvent | None:
        """取一条事件；超时返回 None，HTTP 层据此发心跳（防止中间层断开空闲连接）。"""
        try:
            return self.queue.get(timeout=timeout)
        except queue.Empty:
            return None

    def close(self) -> None:
        """标记关闭并推入哨兵 None，让正在等待的读取方立刻退出。"""
        self.closed = True
        try:
            self.queue.put_nowait(None)
        except queue.Full:
            pass


class EventBus:
    def __init__(self, replay: int = REPLAY) -> None:
        self._lock = Lock()
        self._subscribers: dict[int, Subscription] = {}
        self._next_id = count(1)
        self._replay: deque[PipelineEvent] = deque(maxlen=replay)
        # Event ids are per-process; the boot id keeps a reconnecting browser from
        # comparing ids across a server restart and silently dropping new frames.
        self.boot_id = uuid4().hex[:8]

    def publish(self, pipeline_id: str, event_type: str, **data: Any) -> PipelineEvent:
        """发布事件：先入库用于回放，再投递给订阅了"全部"或该方案的订阅者。"""
        with self._lock:
            event = PipelineEvent(next(self._next_id), pipeline_id, event_type, data)
            self._replay.append(event)
            targets = [
                subscription
                for subscription in self._subscribers.values()
                if subscription.pipeline_id in {None, pipeline_id}
            ]
        for subscription in targets:
            subscription.offer(event)
        return event

    def frame_id(self, event: PipelineEvent) -> str:
        """对外的帧号：``<boot_id>-<event_id>``，用于跨重启的事件 id 比较。"""
        return f"{self.boot_id}-{event.event_id}"

    def _resume_point(self, last_event_id: str | None) -> int:
        """解析 ``Last-Event-ID``：同一次进程启动内的 id 才按号续传，否则从头回放。"""
        if not last_event_id:
            return 0
        prefix, _, value = str(last_event_id).partition("-")
        if prefix == self.boot_id and value.isdigit():
            return int(value)
        return 0  # A foreign/older id replays the whole bounded buffer instead of skipping.

    def subscribe(
        self, pipeline_id: str | None = None, last_event_id: str | None = None
    ) -> tuple[Subscription, list[PipelineEvent]]:
        """注册订阅者，并返回需要补发的事件（用于浏览器断线重连）。"""
        subscription = Subscription(pipeline_id)
        resume_from = self._resume_point(last_event_id)
        with self._lock:
            replay = [
                event
                for event in self._replay
                if event.event_id > resume_from and pipeline_id in {None, event.pipeline_id}
            ]
            self._subscribers[id(subscription)] = subscription
        return subscription, replay

    def unsubscribe(self, subscription: Subscription) -> None:
        with self._lock:
            self._subscribers.pop(id(subscription), None)
        subscription.close()

    def close(self) -> None:
        """关闭全部订阅者（服务停止时调用），避免残留的等待线程。"""
        with self._lock:
            subscriptions = list(self._subscribers.values())
            self._subscribers.clear()
        for subscription in subscriptions:
            subscription.close()

    @property
    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subscribers)

    def recent(self, pipeline_id: str | None = None, limit: int = 50) -> list[PipelineEvent]:
        """读取最近的若干条事件（调试与 HTTP 轮询接口使用）。"""
        with self._lock:
            events = [event for event in self._replay if pipeline_id in {None, event.pipeline_id}]
        return events[-max(1, min(limit, REPLAY)) :]
