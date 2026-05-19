"""Agent connection registry + multiplexed request/response over WebSocket.

Wire protocol (JSON messages, both directions, plus binary chunks):

    Agent -> Hub:
      {"type": "auth", "device_id": "...", "token": "..."}
      {"type": "response", "id": "<rid>", "ok": true, "result": {...}}
      {"type": "response", "id": "<rid>", "ok": false, "error": "..."}
      {"type": "stream_start", "id": "<rid>", "size": <int>, "content_type": "..."}
      {"type": "stream_end",   "id": "<rid>"}

      V2.1+: chunks are sent as a text header followed by a binary frame,
             paired atomically under the agent's send-lock so multiplexed
             streams stay disambiguated:
        {"type": "stream_chunk_header", "id": "<rid>"}
        <binary frame: raw chunk bytes>

      V2.0 (legacy, still accepted): base64-encoded inside JSON:
        {"type": "stream_chunk", "id": "<rid>", "data": "<base64>"}

    Hub -> Agent:
      {"type": "auth_ok"} | {"type": "auth_fail", "error": "..."}
      {"type": "request", "id": "<rid>", "op": "...", "args": {...}}

      V3.0 upload: hub may stream chunks *to* the agent (write_file op).
      Same wire format as the agent's chunk uploads, just reversed direction:
        {"type": "stream_chunk_header", "id": "<rid>"}
        <binary frame>
        ...
        {"type": "stream_end", "id": "<rid>"}
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
        # V2.1 binary-chunk protocol: when we see {"type":"stream_chunk_header",
        # "id": rid}, the very next binary frame on this connection belongs to
        # `rid`. We rely on the agent's send-lock keeping (header, binary)
        # atomic so this never gets confused across multiplexed streams.
        self._pending_binary_for_rid: Optional[str] = None
        # V5.16: latest direct-connect advertisement from this agent
        # ({port, hosts:[...], ticket}) so the hub can hand a browser the
        # device's LAN/mesh address for a low-latency direct session.
        self.direct_info: Optional[dict] = None

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

    async def upload(self, op: str, args: dict,
                     chunks: AsyncIterator[bytes],
                     timeout: float = 600.0) -> dict:
        """Send an upload-style request: an initial JSON request frame, a
        sequence of (header, binary) chunk pairs streamed *to* the agent,
        then a stream_end. Awaits a single response. Used for write_file.

        The send-lock keeps each (header, binary) pair atomic so the agent's
        receive routing isn't confused across multiplexed sends.
        """
        rid = uuid.uuid4().hex
        loop = asyncio.get_event_loop()
        fut: asyncio.Future = loop.create_future()
        self._pending_unary[rid] = fut
        try:
            await self._send({"type": "request", "id": rid, "op": op,
                              "args": args or {}})
            async for chunk in chunks:
                if not chunk:
                    continue
                async with self._send_lock:
                    await self.ws.send_json({"type": "stream_chunk_header",
                                             "id": rid})
                    await self.ws.send_bytes(chunk)
            await self._send({"type": "stream_end", "id": rid})
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
        """Dispatch a TEXT message received from the agent to its waiter."""
        rid = msg.get("id")
        mtype = msg.get("type")
        if mtype == "direct_info":
            self.direct_info = {
                "port": msg.get("port"),
                "hosts": msg.get("hosts") or [],
                "ticket": msg.get("ticket"),
            }
            return
        if mtype == "response":
            # Unary call waiting on this rid? Resolve it.
            fut = self._pending_unary.get(rid)
            if fut and not fut.done():
                fut.set_result(msg)
                return
            # No unary waiter -- this is the agent telling us a STREAM op
            # failed before it could send any stream frames (e.g.,
            # camera_capture raising RuntimeError because termux-api is
            # missing). Forward to the stream queue so stream() raises
            # AgentError instead of waiting for a stream_start that will
            # never come.
            q = self._pending_stream.get(rid)
            if q is not None:
                await q.put(msg)
            return
        if mtype == "stream_chunk_header":
            # Arm the connection: the next binary frame is this rid's chunk.
            self._pending_binary_for_rid = rid
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

    async def handle_incoming_binary(self, data: bytes) -> None:
        """A raw chunk frame arrived; it belongs to whichever rid the most
        recent stream_chunk_header named. The agent's send-lock guarantees
        that pairing is unambiguous."""
        rid = self._pending_binary_for_rid
        self._pending_binary_for_rid = None
        if rid is None:
            return  # unsolicited binary frame; ignore
        q = self._pending_stream.get(rid)
        if q is None:
            return  # request was cancelled / timed out
        # Stuff the bytes into the same envelope shape the legacy base64
        # path uses, with a "_binary" key the consumer can prefer.
        await q.put({"type": "stream_chunk", "id": rid, "_binary": data})

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
