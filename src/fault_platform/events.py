"""In-process publish/subscribe bridge between the runtime and the designer.

The platform is a single local process, so a bounded in-memory bus is enough:
the execution engine publishes state transitions, the HTTP layer forwards them
to web pages as Server-Sent Events. Slow readers never block the runtime and
late subscribers can replay the recent past.
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
    event_id: int
    pipeline_id: str
    type: str
    data: dict[str, Any]

    def to_sse(self, frame_id: str | None = None) -> str:
        """One Server-Sent Events frame; the type travels inside the JSON payload."""
        payload = {"type": self.type, "pipeline_id": self.pipeline_id, "event_id": self.event_id, **self.data}
        identifier = frame_id or str(self.event_id)
        return f"id: {identifier}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


class Subscription:
    """One reader (a browser connection) with a bounded backlog."""

    def __init__(self, pipeline_id: str | None) -> None:
        self.pipeline_id = pipeline_id
        self.queue: queue.Queue[PipelineEvent | None] = queue.Queue(maxsize=MAX_QUEUE)
        self.closed = False

    def offer(self, event: PipelineEvent) -> None:
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
        try:
            return self.queue.get(timeout=timeout)
        except queue.Empty:
            return None

    def close(self) -> None:
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
        return f"{self.boot_id}-{event.event_id}"

    def _resume_point(self, last_event_id: str | None) -> int:
        if not last_event_id:
            return 0
        prefix, _, value = str(last_event_id).partition("-")
        if prefix == self.boot_id and value.isdigit():
            return int(value)
        return 0  # A foreign/older id replays the whole bounded buffer instead of skipping.

    def subscribe(
        self, pipeline_id: str | None = None, last_event_id: str | None = None
    ) -> tuple[Subscription, list[PipelineEvent]]:
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
        with self._lock:
            events = [event for event in self._replay if pipeline_id in {None, event.pipeline_id}]
        return events[-max(1, min(limit, REPLAY)) :]
