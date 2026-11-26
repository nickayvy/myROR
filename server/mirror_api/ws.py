"""WebSocket hub: push typed events to the mirror and paired phones.

    await hub.broadcast("today.updated", {"date": "2026-11-25"})
    await hub.broadcast("capture.state", {"state": "countdown"}, audience="mirror")

Wire format, every message: {"type": str, "data": any, "at": ISO-8601}.
Event names are dotted, `<area>.<what>`. Clients may send {"type": "ping"} and get
{"type": "pong"}.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

from fastapi import WebSocket

from mirror_api.contracts import WsEvent

log = logging.getLogger(__name__)

Audience = Literal["all", "mirror", "phone"]
ClientKind = Literal["mirror", "phone"]


@dataclass(eq=False)
class WsClient:
    ws: WebSocket
    kind: ClientKind
    device_id: str | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class Hub:
    def __init__(self, send_timeout_s: float = 5.0) -> None:
        self._clients: set[WsClient] = set()
        self._send_timeout = send_timeout_s

    @property
    def counts(self) -> dict[str, int]:
        out = {"mirror": 0, "phone": 0}
        for c in self._clients:
            out[c.kind] += 1
        return out

    def add(self, client: WsClient) -> None:
        self._clients.add(client)

    def remove(self, client: WsClient) -> None:
        self._clients.discard(client)

    @staticmethod
    def envelope(type_: str, data: Any = None) -> dict[str, Any]:
        return WsEvent(type=type_, data=data, at=datetime.now(UTC)).model_dump(mode="json")

    async def broadcast(self, type_: str, data: Any = None, *, audience: Audience = "all") -> int:
        """Send to every matching client; returns how many received it. Dead clients
        are dropped. Never raises."""
        msg = self.envelope(type_, data)
        targets = [c for c in self._clients if audience == "all" or c.kind == audience]
        results = await asyncio.gather(*(self._send(c, msg) for c in targets))
        return sum(results)

    async def send(self, client: WsClient, type_: str, data: Any = None) -> bool:
        return await self._send(client, self.envelope(type_, data))

    async def disconnect_device(self, device_id: str) -> None:
        """Close sockets of a revoked device."""
        for c in [c for c in self._clients if c.device_id == device_id]:
            self.remove(c)
            try:
                await c.ws.close(code=4401)
            except Exception:  # noqa: BLE001 - already gone
                pass

    async def close_all(self) -> None:
        for c in list(self._clients):
            self.remove(c)
            try:
                await c.ws.close(code=1001)
            except Exception:  # noqa: BLE001
                pass

    async def _send(self, client: WsClient, msg: dict[str, Any]) -> bool:
        try:
            async with client.lock:
                await asyncio.wait_for(client.ws.send_json(msg), self._send_timeout)
            return True
        except Exception as e:  # noqa: BLE001 - a broken socket must not break a broadcast
            log.info("dropping ws client (%s): %s", client.kind, e)
            self.remove(client)
            return False
