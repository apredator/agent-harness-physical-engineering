"""Общий агентский цикл: инструкция → инструменты → результат.

Демо-конвейер (contract → v0 → work → verify) хорош для известного класса
задач, но PLE даёт произвольные инструкции. Здесь — универсальный цикл:
модель получает инструменты и работает, пока не скажет «готово».

Протокол общения — текстовый JSON, а не нативный tool-calling: так он
одинаково работает у DeepSeek, YandexGPT, Gemma и любых OpenAI-совместимых
провайдеров, включая те, что не поддерживают function calling.
"""
from __future__ import annotations

import json
import re

from .budget import Budget
from .protocol import Cancelled, MCPClient, Session, TaskError

MAX_STEPS = 150
DRAFT_DEADLINE = 12      # к этому шагу деливерабл обязан существовать
OUTPUT_LIMIT = 4000
FILE_LIMIT = 8000
MAX_HISTORY = 14

SYSTEM = """Ты инженер-исполнитель. Тебе дали рабочее задание и доступ к контейнеру
с проектом. Твоя цель — выполнить задание и оставить готовые файлы.

Инструменты. Отвечай РОВНО ОДНИМ JSON-объектом, без текста до и после:

{"tool": "exec", "command": "bash-команда"}
{"tool": "read_file", "path": "/app/путь"}
{"tool": "write_file", "path": "/app/путь", "content": "содержимое"}
{"tool": "list_dir", "path": "/app"}
{"tool": "mcp_list", "server": "apps"}
{"tool": "mcp_call", "server": "apps", "name": "docs.search", "arguments": {"query": "..."}}
{"tool": "finish", "summary": "что сделано и где лежит"}

СТРАТЕГИЯ (обязательна):
* Шаги 1-8 — осмотр: list_dir, чтение оглавления и ключевых файлов.
* Шаг 9 — выпиши для себя пути файлов-деливераблов из задания.
* Шаг 10-12 — ЗАПИШИ ЧЕРНОВИК деливерабла по этим путям, даже с неполными данными.
  Файл на диске важнее идеального расчёта: незаписанный ответ оценивается в ноль.
* Дальше — уточняй черновик, перезаписывая его.
* Не читай больше трёх источников подряд, не записав ничего.

Правила:
1. Сначала осмотрись: list_dir и чтение файлов, прежде чем считать.
2. Все числа бери из файлов проекта, не выдумывай.
3. Единицы измерения — из задания; проверяй их.
4. Файлы-деливераблы клади строго по путям из задания.
5. Если инструмент вернул ошибку — попробуй другой способ, не повторяй то же самое.
6. Когда задание выполнено — ответь {"tool": "finish", ...}.
7. Отвечай только JSON. Никаких объяснений вне JSON.
"""


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit // 2] + "\n...[обрезано]...\n" + text[-limit // 2:]


def parse_action(text: str) -> dict:
    """Достать JSON-действие из ответа модели."""
    clean = re.sub(r"<(thought|thinking)>.*?</\1>", "", text, flags=re.S | re.I)
    clean = re.sub(r"^```(?:json)?\s*|\s*```$", "", clean.strip())
    # ищем первый сбалансированный объект
    start = clean.find("{")
    if start < 0:
        return {"tool": "invalid", "raw": clean[:300]}
    depth, in_str, esc = 0, False, False
    for i in range(start, len(clean)):
        ch = clean[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        else:
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        data = json.loads(clean[start:i + 1])
                        if isinstance(data, dict) and "tool" in data:
                            return data
                    except json.JSONDecodeError:
                        break
                    return {"tool": "invalid", "raw": clean[start:i + 1][:300]}
    return {"tool": "invalid", "raw": clean[start:start + 300]}


class Agent:
    def __init__(self, session: Session, model, budget: Budget):
        self.s = session
        self.model = model
        self.b = budget
        self.history: list[dict] = []
        self.log: list[str] = []
        self.writes = 0
        self._mcp: dict[str, MCPClient] = {}
        self._mcp_tools: dict[str, list] = {}

    async def say(self, text: str) -> None:
        self.log.append(text)
        await self.s.log(text[:1500])

    async def execute(self, action: dict) -> str:
        tool = action.get("tool")
        try:
            if tool == "exec":
                r = await self.s.exec(str(action.get("command", "")), int(action.get("timeout_sec") or 120))
                body = r.output or ""
                return f"exit={r.exit_code}{' TIMEOUT' if r.timed_out else ''}\n{_truncate(body, OUTPUT_LIMIT)}"
            if tool == "read_file":
                data = await self.s.read_file(str(action.get("path", "")))
                return _truncate(data.decode("utf-8", "replace"), FILE_LIMIT)
            if tool == "write_file":
                content = action.get("content", "")
                if not isinstance(content, str):
                    content = json.dumps(content, ensure_ascii=False)
                await self.s.write_file(str(action.get("path", "")), content)
                self.writes += 1
                return f"записано {len(content)} символов в {action.get('path')}"
            if tool == "list_dir":
                path = str(action.get("path") or self.s.workdir)
                r = await self.s.exec(f"ls -la {json.dumps(path)} | head -60", 60)
                return _truncate(r.output, OUTPUT_LIMIT)
            if tool in ("mcp_list", "mcp_call"):
                return await self._mcp_action(action)
            if tool == "invalid":
                return ("ОШИБКА ФОРМАТА: нужен один JSON-объект с полем tool. "
                        "Допустимые tool: exec, read_file, write_file, list_dir, finish.")
            return f"неизвестный инструмент {tool!r}"
        except TaskError as e:
            return f"ОШИБКА ИНСТРУМЕНТА: {e}"
        except Cancelled:
            raise

    def _client(self, name: str) -> MCPClient:
        if name not in self._mcp:
            servers = {s.get("name"): s for s in self.s.mcp_servers}
            if name not in servers:
                raise TaskError(f"нет MCP-сервера {name!r}; доступны {list(servers)}")
            self._mcp[name] = MCPClient(self.s, servers[name]["url"])
        return self._mcp[name]

    async def _mcp_action(self, action: dict) -> str:
        """Работа с task-MCP: сначала каталог, потом вызов конкретного инструмента."""
        name = str(action.get("server") or "")
        try:
            client = self._client(name)
            if action.get("tool") == "mcp_list":
                if name not in self._mcp_tools:
                    await client.initialize()
                    self._mcp_tools[name] = await client.list_tools()
                tools = self._mcp_tools[name]
                brief = [{"name": t.get("name"), "desc": (t.get("description") or "")[:110]}
                         for t in tools]
                return _truncate(json.dumps(brief, ensure_ascii=False), FILE_LIMIT)
            if name not in self._mcp_tools:
                await client.initialize()
                self._mcp_tools[name] = await client.list_tools()
            result = await client.call_tool(str(action.get("name") or ""),
                                            action.get("arguments") or {})
            return _truncate(json.dumps(result, ensure_ascii=False), OUTPUT_LIMIT)
        except TaskError as e:
            return f"ОШИБКА MCP: {e}"

    def compact(self) -> None:
        """Держим историю короткой: первые два сообщения + последние."""
        if len(self.history) <= MAX_HISTORY:
            return
        head = self.history[:2]
        tail = self.history[-(MAX_HISTORY - 3):]
        summary = {"role": "user",
                   "content": "[сжатие] предыдущие шаги опущены; продолжай с текущего состояния"}
        self.history = head + [summary] + tail

    async def load_spec(self, workdir: str) -> str:
        """Найти и прочитать главный документ задания — он экономит десятки шагов."""
        try:
            found = await self.s.exec(
                f"find {workdir} -maxdepth 2 -name 'task.md' -o -maxdepth 2 -name 'README.md' "
                f"| head -1", 30)
            path = (found.output or "").strip().splitlines()
            if not path:
                return ""
            data = await self.s.read_file(path[0])
            text = data.decode("utf-8", "replace")
            return _truncate(text, 20000)
        except (TaskError, Cancelled):
            return ""

    async def run(self, instruction: str, workdir: str) -> str:
        listing = await self.execute({"tool": "list_dir", "path": workdir})
        spec = await self.load_spec(workdir)
        if spec:
            await self.say(f"спецификация загружена: {len(spec)} символов")
        if self.s.mcp_servers:
            names = ", ".join(sv.get("name", "?") for sv in self.s.mcp_servers)
            listing += (f"\n\nДОСТУПНЫ MCP-СЕРВЕРЫ: {names}. "
                        "Инструменты: mcp_list (каталог), mcp_call (вызов).")
        self.history = [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": f"ЗАДАНИЕ:\n{instruction}\n\n"
                                        f"РАБОЧИЙ КАТАЛОГ: {workdir}\n\n"
                                        f"СОДЕРЖИМОЕ:\n{listing}"
                                        + (f"\n\nГЛАВНЫЙ ДОКУМЕНТ ЗАДАНИЯ:\n{spec}" if spec else "")},
        ]
        summary = "Задание не завершено: шаги закончились."
        for step in range(1, MAX_STEPS + 1):
            if self.b.over():
                await self.say(f"шаг {step}: бюджет исчерпан, останавливаюсь")
                summary = "Задание не завершено: исчерпан бюджет."
                break
            text, usage = await self.model.complete(self.history, max_tokens=2500)
            await self.s.usage_add(getattr(self.model, "name", "unknown"),
                                   usage.get("input_tokens", 0),
                                   usage.get("output_tokens", 0),
                                   usage.get("cache_read_tokens", 0))
            self.b.note_tokens(usage)

            wrote = any(h.get("content", "").startswith("записано") for h in self.history[-2:])
            status = (f"СТАТУС: шаг {step} из {MAX_STEPS}. Записано файлов: {self.writes}. "
                      f"Осталось ~{max(0, MAX_STEPS - step)} шагов.")
            if self.writes == 0 and step >= DRAFT_DEADLINE:
                status += ("\n⚠️ Ты ещё НЕ ЗАПИСАЛ НИ ОДНОГО ФАЙЛА. Задача будет оценена в ноль. "
                           "Немедленно запиши черновик деливерабла через write_file.")
            elif self.writes == 0 and step >= DRAFT_DEADLINE - 4:
                status += "\n⚠️ Приближается момент, когда деливерабл обязан быть на диске."
            self.history.append({"role": "user", "content": status})

            action = parse_action(text)
            tool = action.get("tool")
            if tool == "finish":
                summary = str(action.get("summary") or "готово")
                await self.say(f"шаг {step}: finish — {summary[:200]}")
                break

            result = await self.execute(action)
            await self.say(f"шаг {step}: {tool} → {result[:160]!r}")
            self.history.append({"role": "assistant", "content": _truncate(text, 1500)})
            self.history.append({"role": "user", "content": f"РЕЗУЛЬТАТ {tool}:\n{result}"})
            self.compact()
        return summary
