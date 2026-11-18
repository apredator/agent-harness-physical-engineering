"""Протокол serve v1 со стороны харнесса.

Харнесс — это WebSocket-сервер. Эвалюатор подключается, присылает задачу
и исполняет наши запросы. Здесь — тонкая обёртка: Session (exec/read/write/http
+ односторонние кадры) и MCPClient (JSON-RPC поверх http-запроса).
"""
from __future__ import annotations

import asyncio
import base64
import json
import time
import uuid
from dataclasses import dataclass


class Cancelled(Exception):
    """Эвалюатор прислал cancel: время вышло (или соединение закрылось)."""


class TaskError(Exception):
    """Запрос не выполнен: ok=false, таймаут или отказ контейнера."""


@dataclass
class ExecResult:
    exit_code: int
    output: str
    truncated: bool = False
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out


class Session:
    """Одно соединение = одна задача."""

    MAX_CONCURRENT = 8          # лимит протокола: больше 8 запросов эвалюатор не читает
    MAX_WRITE = 10 * 1024 * 1024  # 10 МБ на один write_file

    def __init__(self, ws, task: dict):
        self.ws = ws
        self.task = task
        self.instruction: str = task["instruction"]
        self.workdir: str = task.get("workdir", "/app")
        self.limits: dict = task.get("limits") or {}
        self.mcp_servers: list = task.get("mcp_servers") or []
        self.cancelled = False
        self.usage = {"input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0}
        self._deadline = None
        if self.limits.get("agent_timeout_sec"):
            self._deadline = time.monotonic() + float(self.limits["agent_timeout_sec"])
        self._pending: dict[str, asyncio.Future] = {}
        self._sem = asyncio.Semaphore(self.MAX_CONCURRENT)
        self._reader = asyncio.create_task(self._read_loop())

    # --- служебное ---------------------------------------------------------
    def time_left(self) -> float | None:
        if self._deadline is None:
            return None
        return max(0.0, self._deadline - time.monotonic())

    async def _read_loop(self) -> None:
        try:
            async for raw in self.ws:
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                kind = msg.get("type")
                if kind == "result":
                    fut = self._pending.pop(msg.get("id"), None)
                    if fut and not fut.done():
                        fut.set_result(msg)
                elif kind == "cancel":
                    # время вышло: снимаем все ожидания, но сокет ещё жив ~10 с
                    self.cancelled = True
                    self._fail_pending(Cancelled("cancelled: the time is up"))
        except Exception:
            pass
        finally:
            self._fail_pending(Cancelled("connection closed"))

    def _fail_pending(self, exc: Exception) -> None:
        for fut in list(self._pending.values()):
            if not fut.done():
                fut.set_exception(exc)
        self._pending.clear()

    async def _request(self, payload: dict, timeout: float) -> dict:
        if self.cancelled:
            raise Cancelled("cancelled: the time is up")
        async with self._sem:
            rid = uuid.uuid4().hex[:16]
            fut = asyncio.get_running_loop().create_future()
            self._pending[rid] = fut
            try:
                await self.ws.send(json.dumps({**payload, "id": rid}))
                res = await asyncio.wait_for(fut, timeout=timeout)
            except asyncio.TimeoutError:
                self._pending.pop(rid, None)
                raise TaskError(f"no reply to {payload.get('type')} within {timeout:.0f}s")
            if res.get("ok") is False:
                raise TaskError(res.get("error", "request failed"))
            return res

    # --- четыре операции протокола ----------------------------------------
    async def exec(self, command: str, timeout_sec: int = 120) -> ExecResult:
        res = await self._request(
            {"type": "exec", "command": command, "timeout_sec": int(timeout_sec)},
            timeout=timeout_sec + 30,
        )
        return ExecResult(
            exit_code=int(res.get("exit_code", 0)),
            output=res.get("output", "") or "",
            truncated=bool(res.get("truncated", False)),
            timed_out=bool(res.get("timed_out", False)),
        )

    async def read_file(self, path: str) -> bytes:
        res = await self._request({"type": "read_file", "path": path}, timeout=120)
        return base64.b64decode(res.get("content_b64") or "")

    async def write_file(self, path: str, data: bytes | str, append: bool = False) -> None:
        if isinstance(data, str):
            data = data.encode()
        if len(data) > self.MAX_WRITE:
            raise TaskError(f"write_file too large: {len(data)} bytes")
        await self._request(
            {
                "type": "write_file",
                "path": path,
                "content_b64": base64.b64encode(data).decode(),
                "append": append,
            },
            timeout=120,
        )

    async def http(self, url: str, method: str = "POST", headers: dict | None = None,
                   body: bytes | None = None, timeout_sec: int = 120) -> dict:
        return await self._request(
            {
                "type": "http",
                "method": method,
                "url": url,
                "headers": headers or {},
                "body_b64": base64.b64encode(body or b"").decode(),
            },
            timeout=timeout_sec,
        )

    # --- односторонние -----------------------------------------------------
    async def log(self, text: str) -> None:
        try:
            await self.ws.send(json.dumps({"type": "log", "text": str(text)[:4000]}))
        except Exception:
            pass

    async def usage_add(self, model: str, input_tokens: int, output_tokens: int,
                        cache_read_tokens: int = 0) -> None:
        self.usage["input_tokens"] += int(input_tokens)
        self.usage["output_tokens"] += int(output_tokens)
        self.usage["cache_read_tokens"] += int(cache_read_tokens)
        try:
            await self.ws.send(json.dumps({
                "type": "usage", "model": model,
                "input_tokens": int(input_tokens), "output_tokens": int(output_tokens),
                "cache_read_tokens": int(cache_read_tokens),
            }))
        except Exception:
            pass

    async def done(self, final_message: str) -> None:
        try:
            await self.ws.send(json.dumps({"type": "done", "final_message": final_message}))
        except Exception:
            pass

    async def error(self, message: str) -> None:
        try:
            await self.ws.send(json.dumps({"type": "error", "message": message}))
        except Exception:
            pass

    async def close(self) -> None:
        self._reader.cancel()


class MCPClient:
    """Клиент task-MCP: Streamable HTTP, JSON-RPC 2.0, JSON или SSE в ответе."""

    def __init__(self, session: Session, url: str):
        self.session = session
        self.url = url
        self.session_id: str | None = None
        self.protocol_version: str | None = None
        self._id = 0

    def _headers(self) -> dict:
        h = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if self.session_id:
            h["Mcp-Session-Id"] = self.session_id
        if self.protocol_version:
            h["MCP-Protocol-Version"] = self.protocol_version
        return h

    @staticmethod
    def _decode(payload: bytes, headers: dict) -> dict:
        text = payload.decode("utf-8", "replace")
        ctype = " ".join(f"{k}: {v}" for k, v in (headers or {}).items()).lower()
        if "text/event-stream" in ctype or text.lstrip().startswith("event:") \
                or "\ndata:" in text:
            for line in text.splitlines():
                if line.startswith("data:"):
                    try:
                        return json.loads(line[5:].strip())
                    except json.JSONDecodeError:
                        continue
            return {}
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return {}

    async def rpc(self, method: str, params: dict | None = None) -> dict:
        self._id += 1
        body = json.dumps({"jsonrpc": "2.0", "id": self._id,
                           "method": method, "params": params or {}}).encode()
        res = await self.session.http(self.url, "POST", self._headers(), body)
        headers = res.get("headers") or {}
        for k, v in headers.items():
            if k.lower() == "mcp-session-id":
                self.session_id = v
        out = self._decode(base64.b64decode(res.get("body_b64") or ""), headers)
        if "error" in out:
            raise TaskError(f"MCP {method}: {out['error']}")
        return out.get("result", out)

    async def initialize(self) -> dict:
        result = await self.rpc("initialize", {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "ple-harness", "version": "0.1"},
        })
        self.protocol_version = result.get("protocolVersion", "2025-06-18")
        # уведомление без ответа — допускается протоколом; шлём best-effort
        return result

    async def list_tools(self) -> list:
        result = await self.rpc("tools/list", {})
        return result.get("tools", [])

    async def call_tool(self, name: str, arguments: dict) -> dict:
        result = await self.rpc("tools/call", {"name": name, "arguments": arguments})
        return result
