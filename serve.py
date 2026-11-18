#!/usr/bin/env python3
"""Точка входа харнесса: WebSocket-сервер на $PORT.

Контракт с эвалюатором ровно один — протокол serve v1. Всё остальное
(модель, шаги, бюджеты) — внутреннее дело харнесса.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys

from websockets.asyncio.server import serve

from ple.agent import Agent
from ple.budget import Budget
from ple.loop import run as run_demo
from ple.model import FakeModel, OpenAIModel
from ple.protocol import Cancelled, Session

MAX_FRAME = 16 * 1024 * 1024


def build_model():
    """Реальная модель — если есть MODEL_BASE_URL, иначе детерминированный стенд-ин.

    В заявке модель задаётся в submission.yaml; в контейнере организаторы
    кладут URL и ключ в MODEL_BASE_URL / MODEL_API_KEY.
    """
    base = os.environ.get("MODEL_BASE_URL")
    if os.environ.get("PLE_FAKE") == "1" or not base:
        return FakeModel(inject=os.environ.get("FAKE_INJECT", ""))
    return OpenAIModel(
        base_url=base,
        api_key=os.environ.get("MODEL_API_KEY", ""),
        model=os.environ.get("MODEL_NAME", "claude-opus-5-5"),
        timeout=float(os.environ.get("MODEL_TIMEOUT", "300")),
        auth_scheme=os.environ.get("MODEL_AUTH_SCHEME", "Bearer"),
        reasoning_effort=os.environ.get("MODEL_REASONING_EFFORT", ""),
    )


async def handler(ws) -> None:
    model = build_model()
    try:
        raw = await asyncio.wait_for(ws.recv(), timeout=60)
        task = json.loads(raw)
        if task.get("type") != "task":
            raise ValueError("first frame must be a task")
    except Exception as e:  # noqa: BLE001
        print(f"bad task frame: {e}", file=sys.stderr, flush=True)
        await ws.close()
        return

    session = Session(ws, task)
    budget = Budget((task.get("limits") or {}).get("agent_timeout_sec"))

    # agent — универсальный цикл (по умолчанию); demo — конвейер под синтетику
    mode = os.environ.get("PLE_MODE", "agent")
    try:
        if mode == "demo":
            await run_demo(session, model, budget)
        else:
            agent = Agent(session, model, budget)
            try:
                summary = await agent.run(session.instruction, session.workdir)
            except Cancelled:
                summary = "Время вышло; файлы, записанные до отмены, сохранены."
            await session.done(summary)
    finally:
        await session.close()


async def main() -> None:
    port = int(os.environ.get("PORT", "8080"))
    async with serve(handler, "0.0.0.0", port, max_size=MAX_FRAME, ping_interval=None):
        print(f"harness listening on 0.0.0.0:{port}", flush=True)
        await asyncio.Future()


if __name__ == "__main__":
    asyncio.run(main())
