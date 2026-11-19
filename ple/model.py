"""Модельный слой: одна точка, через которую харнесс говорит с моделью.

Два бэкенда:
  * OpenAIModel — реальный, OpenAI-совместимый эндпоинт (так работают
    google-cloud/<id> через прокси организаторов);
  * FakeModel  — детерминированный «стенд-ин» для отладки без ключей и сети.

Важно: оба получают один и тот же промпт. Если FakeModel ветвится по фазе,
то и реальная модель получает ту же метку фазы — значит отлаженный на фейке
цикл переносится на реальную модель без переписывания.
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request


def _tokens(text: str) -> int:
    """Грубая оценка токенов — только для отладки бюджета (4 символа ≈ 1 токен)."""
    return max(1, len(text) // 4)


class ModelError(Exception):
    pass


def _content_text(data: dict) -> str:
    """Достаёт текст ответа из любого из двух форматов.

    Шлюз на Stage 2 — LiteLLM перед спонсируемой моделью. Для текстовых ответов он
    отдаёт строку, но у части конфигураций (и у нативного Anthropic-формата) `content`
    приходит списком блоков `[{"type": "text", "text": "..."}]`. Строка-предположение
    ломала разбор действия и превращала рабочий ответ в `invalid`.
    """
    choices = data.get("choices") or []
    if not choices:
        raise KeyError("choices")
    content = (choices[0].get("message") or {}).get("content")
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                parts.append(str(block.get("text") or block.get("content") or ""))
            elif block is not None:
                parts.append(str(block))
        return "".join(parts)
    return content or ""


class OpenAIModel:
    """POST /chat/completions c ретраями. Ключ и URL приходят из окружения."""

    def __init__(self, base_url: str, api_key: str, model: str, timeout: float = 300.0,
                 auth_scheme: str = "Bearer", reasoning_effort: str = ""):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        # некоторые провайдеры требуют свой префикс в Authorization (например, Api-Key)
        self.auth_scheme = auth_scheme or "Bearer"
        self.reasoning_effort = reasoning_effort

    def _post(self, payload: dict) -> dict:
        req = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json",
                     "Authorization": f"{self.auth_scheme} {self.api_key}"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            return json.loads(r.read().decode())

    async def complete(self, messages: list[dict], max_tokens: int = 4000,
                       temperature: float = 0.2) -> tuple[str, dict]:
        import asyncio

        payload = {"model": self.model, "messages": messages,
                   "max_tokens": max_tokens, "temperature": temperature}
        if self.reasoning_effort:
            # «размышляющие» модели иначе тратят весь max_tokens на reasoning
            # и возвращают пустой content
            payload["reasoning_effort"] = self.reasoning_effort
        last = None
        for attempt in range(3):
            try:
                data = await asyncio.to_thread(self._post, payload)
                text = _content_text(data)
                usage = data.get("usage") or {}
                prompt_tok = usage.get("prompt_tokens") or _tokens(str(messages))
                comp_tok = usage.get("completion_tokens") or 0
                total_tok = usage.get("total_tokens") or 0
                if not comp_tok and total_tok > prompt_tok:
                    # Gemini-совместимый эндпоинт часто отдаёт 0 в completion_tokens
                    comp_tok = total_tok - prompt_tok
                return text, {
                    "input_tokens": prompt_tok,
                    "output_tokens": comp_tok or _tokens(text),
                    "cache_read_tokens": (usage.get("prompt_tokens_details") or {})
                                             .get("cached_tokens", 0),
                }
            except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, KeyError) as e:
                last = e
                await asyncio.sleep(1.5 * (attempt + 1))
        raise ModelError(f"model call failed after 3 attempts: {last}")


class FakeModel:
    """Детерминированный «инженер», который умеет ровно то, что нужно демо.

    Читает метку фазы из последнего сообщения и возвращает заготовку.
    Флаг FAKE_INJECT=units имитирует ошибку единиц, чтобы верификатор её поймал.
    """

    name = "fake/stand-in"

    def __init__(self, inject: str = ""):
        self.inject = inject
        self.calls = 0

    @staticmethod
    def _phase(messages: list[dict]) -> str:
        text = messages[-1]["content"] if messages else ""
        m = re.search(r"PHASE:\s*([a-z_]+)", text)
        return m.group(1) if m else "unknown"

    @staticmethod
    def _numbers(text: str) -> dict:
        """Достаём исходные данные из переданного «доказательства»."""
        out = {}
        for key in ("wind_q_kpa", "height_m", "length_m", "wall_capacity_kn",
                    "cost_a", "cost_b", "capacity_a", "capacity_b", "walls"):
            m = re.search(rf"{key}\s*[=:]\s*([0-9.]+)", text)
            if m:
                out[key] = float(m.group(1))
        return out

    async def complete(self, messages: list[dict], max_tokens: int = 4000,
                       temperature: float = 0.2) -> tuple[str, dict]:
        self.calls += 1
        delay = float(os.environ.get("PLE_STEP_DELAY", "0") or 0)
        if delay:
            # искусственная задержка: нужна, чтобы проверить реакцию на cancel
            import asyncio
            await asyncio.sleep(delay)
        phase = self._phase(messages)
        if os.environ.get("FAKE_BAD_JSON") == "1" and phase in ("answers", "optimise"):
            # модель вернула прозу вместо JSON — проверяем, что харнесс не упадёт
            return "Извините, не могу вернуть JSON в этом режиме.", {
                "input_tokens": 10, "output_tokens": 10, "cache_read_tokens": 0}
        evidence = messages[-1]["content"] if messages else ""
        prompt_chars = sum(len(m.get("content", "")) for m in messages)
        reply = self._reply(phase, evidence)
        usage = {"input_tokens": _tokens("x" * prompt_chars),
                 "output_tokens": _tokens(reply), "cache_read_tokens": 0}
        return reply, usage

    def _reply(self, phase: str, evidence: str) -> str:
        if phase == "contract":
            return json.dumps({
                "deliverables": ["/app/out/answers.json", "/app/out/report.md"],
                "hard_constraints": [
                    "все величины в кН",
                    "нагрузка на стену не превышает её несущей способности",
                    "обслужены обе стены",
                ],
                "soft_constraints": [
                    "в отчёте есть разделы «Расчёт», «Ограничения», «Ссылки на нормы»",
                    "указана ссылка на ASCE 7",
                    "записано принятое допущение о высоте",
                ],
                "units": "kN",
                "assumptions": ["высота ветрового давления принята по критериям задачи"],
            }, ensure_ascii=False)

        if phase in ("answers", "repair", "optimise"):
            n = self._numbers(evidence)
            q, h, L, walls = (n.get("wind_q_kpa", 0.6), n.get("height_m", 6.0),
                              n.get("length_m", 60.0), int(n.get("walls", 2)))
            shear = q * h * L
            per_wall = shear / walls
            cap_a, cap_b = n.get("capacity_a", 120.0), n.get("capacity_b", 180.0)
            cost_a, cost_b = n.get("cost_a", 100.0), n.get("cost_b", 160.0)
            # оптимизация: берём самый дешёвый вариант, который проходит по прочности
            cheapest = "A" if per_wall <= cap_a else "B"
            # v1 (answers/repair) перестраховывается и берёт B; optimise — самый дешёвый
            better = cheapest if phase == "optimise" else "B"
            unit, scale = "kN", 1.0
            if self.inject == "units" and phase == "answers":
                unit, scale = "N", 1000.0  # инъекция ошибки единиц
            return json.dumps({
                "diaphragm_shear_kN": round(shear, 2),
                "wall_load_kN": round(per_wall * scale, 2),
                "unit": unit,
                "wall_option": better,
                "walls_served": walls,
                "code_reference": "ASCE 7-22, гл. 26–27",
                "assumption": f"h_eff = {h} м принята по критериям",
            }, ensure_ascii=False)

        if phase == "report":
            return (
                "# Расчёт диафрагмы покрытия на ветровую нагрузку\n\n"
                "## Расчёт\n"
                "Суммарная ветровая сила F = q · h · L. Нагрузка распределена "
                "между стенами поровну.\n\n"
                "## Ограничения\n"
                "Проверено: единицы кН, нагрузка на стену не превышает несущую "
                "способность, обе стены обслужены.\n\n"
                "## Ссылки на нормы\n"
                "ASCE 7-22, гл. 26–27 (ветровые нагрузки).\n\n"
                "## Допущения\n"
                "Высота ветрового давления принята по критериям задачи.\n"
            )

        return "ok"
