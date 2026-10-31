"""In-process pub/sub used to push simulator and agent events to the website."""
from __future__ import annotations

import asyncio
import time
from collections import deque
from typing import Any

from .sim.records import new_id


class EventBus:
    def __init__(self, history: int = 1500) -> None:
        self._subscribers: list[asyncio.Queue] = []
        self._history: deque[dict[str, Any]] = deque(maxlen=history)
        self._seq = 0

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=600)
        self._subscribers.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        if q in self._subscribers:
            self._subscribers.remove(q)

    def emit(self, kind: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        self._seq += 1
        evt = {
            "seq": self._seq,
            "id": new_id("EVT"),
            "kind": kind,
            "ts": time.time(),
            "payload": payload or {},
        }
        self._history.append(evt)
        dead = []
        for q in self._subscribers:
            try:
                q.put_nowait(evt)
            except asyncio.QueueFull:
                dead.append(q)
        for q in dead:
            self.unsubscribe(q)
        return evt

    def history(self, kinds: set[str] | None = None, limit: int = 400) -> list[dict[str, Any]]:
        items = [e for e in self._history if not kinds or e["kind"] in kinds]
        return items[-limit:]

    def clear(self) -> None:
        self._history.clear()
        self._seq = 0


bus = EventBus()
