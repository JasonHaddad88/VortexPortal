"""Agent connection registry + multiplexed request/response over WebSocket.

Wire protocol (JSON messages, both directions):

    Agent -> Hub:
      {"type": "auth", "device_id": "...", "token": "..."}
      {"type": "response", "id": "<rid>", "ok": true, "result": {...}}
      {"type": "response", "id": "<rid>", "ok": false, "error": "..."}
      {"type": "stream_start", "id": "<rid>", "size": <int>, "content_type": "..."}
      {"type": "stream_chunk", "id": "<rid>", "data": "<base64>"}
      {"type": "stream_end",   "id": "<rid>"}

    Hub -> Agent:
      {"type": "auth_ok"} | {"type": "auth_fail", "error": "..."}
      {"type": "request", "id": "<rid>", "op": "...", "args": {...}}

Why base64 in JSON for chunks rather than binary frames: simplicity and
multiplexing safety. ~33% overhead is fine for a control-panel UI; SCP/rsync
remains the right tool for huge transfers.
"""

import asyncio
import uuid
from typing import AsyncIterator, Optional


class AgentConnection:
    """One paired device. Multiplexes many concurrent requests on one WS."""

    def __init__(self, ws, device_id: str, owner_id: int, name: str):
        self.ws = ws
        self.device_id = device_id
        self.owner_id = owner_id
        self.name = name
        self._pending_unary: dict = {}
        self._pending_stream: dict = {}
        self._send_lock = asyncio.Lock()
        self._closed = False

    async def _send(self, msg: dict) -> None:
        async with self._send_lock:
            await self.ws.send_json(msg)

    async def request(self, op: str, args: Optional[dict] = None,
                      timeout: float = 30.0) -> dict:
        """Send a request, await a single response. Raises on timeout/disconnect."""
        rid = uuid.uuid4().hex
        loop = asyncio.get_event_loop()
        fut: asyncio.Future = loop.create_future()
        self._pending_unary[rid] = fut
        try:
            await self._send({"type": "request", "id": rid, "op": op,
                              "args": args or {}})
            msg = await asyncio.wait_for(fut, timeout=timeout)
        finally:
            self._pending_unary.pop(rid, None)

        if not msg.get("ok"):
            raise AgentError(msg.get("error") or "agent error")
        return msg.get("result") or {}

    async def stream(self, op: str, args: Optional[dict] = None,
                     start_timeout: float = 30.0
                     ) -> AsyncIterator[dict]:
        """Send a streaming request. Yields the stream_start dict first, then
        each stream_chunk dict. Caller is responsible for decoding base64 data.
        """
        rid = uuid.uuid4().hex
        q: asyncio.Queue = asyncio.Queue()
        self._pending_stream[rid] = q
        started = False
        try:
            await self._send({"type": "request", "id": rid, "op": op,
                              "args": args or {}})
            while True:
                if not started:
                    msg = await asyncio.wait_for(q.get(), timeout=start_timeout)
                else:
                    msg = await q.get()
                if msg is None:
                    return
                if msg.get("type") == "stream_start":
                    started = True
                if not msg.get("ok", True) and msg.get("type") == "response":
                    raise AgentError(msg.get("error") or "agent error")
                yield msg
                if msg.get("type") == "stream_end":
                    return
        finally:
            self._pending_stream.pop(rid, None)

    async def handle_incoming(self, msg: dict) -> None:
        """Dispatch a message received from the agent to its waiter."""
        rid = msg.get("id")
        mtype = msg.get("type")
        if mtype == "response":
            fut = self._pending_unary.get(rid)
            if fut and not fut.done():
                fut.set_result(msg)
            return
        if mtype in ("stream_start", "stream_chunk", "stream_end"):
            q = self._pending_stream.get(rid)
            if q is not None:
                await q.put(msg)
            return
        # Unknown messages — also try stream queue in case it's an error frame
        if rid:
            q = self._pending_stream.get(rid)
            if q is not None:
                await q.put(msg)

    async def close_pending(self, error: str = "agent disconnected") -> None:
        """Fail every in-flight request when the connection drops."""
        self._closed = True
        for fut in list(self._pending_unary.values()):
            if not fut.done():
                fut.set_exception(AgentError(error))
        for q in list(self._pending_stream.values()):
            await q.put(None)


class AgentError(Exception):
    """Agent-side error (op failed, disconnect mid-request, timeout, etc.)."""


class Registry:
    """device_id -> AgentConnection. One connection per device at a time."""

    def __init__(self) -> None:
        self._conns: dict = {}
        self._lock = asyncio.Lock()

    async def register(self, conn: AgentConnection) -> None:
        async with self._lock:
            old = self._conns.get(conn.device_id)
            if old is not None and old is not conn:
                try:
                    await old.ws.close(code=1000, reason="superseded")
                except Exception:
                    pass
                await old.close_pending("superseded by new connection")
            self._conns[conn.device_id] = conn

    async def unregister(self, conn: AgentConnection) -> None:
        async with self._lock:
            cur = self._conns.get(conn.device_id)
            if cur is conn:
                del self._conns[conn.device_id]
        await conn.close_pending()

    def get(self, device_id: str) -> Optional[AgentConnection]:
        return self._conns.get(device_id)

    def online_ids(self) -> set:
        return set(self._conns.keys())


registry = Registry()
