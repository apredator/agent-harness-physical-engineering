"""Contract-first: превращаем прозу инструкции в машинный контракт.

Это первый и самый дешёвый шаг. Без него каждый следующий модуль
догадывается по-своему, что считать деливераблом и что считается приёмкой.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field


@dataclass
class Contract:
    deliverables: list[str] = field(default_factory=list)
    hard: list[str] = field(default_factory=list)
    soft: list[str] = field(default_factory=list)
    units: str = ""
    assumptions: list[str] = field(default_factory=list)
    raw: dict = field(default_factory=dict)

    def as_context(self) -> str:
        """Короткая форма для промптов: 10 строк вместо 10 КБ инструкции."""
        return (
            "КОНТРАКТ ЗАДАЧИ\n"
            f"деливераблы: {', '.join(self.deliverables)}\n"
            f"единицы: {self.units}\n"
            f"жёсткие: {'; '.join(self.hard)}\n"
            f"мягкие: {'; '.join(self.soft)}\n"
            f"допущения: {'; '.join(self.assumptions)}\n"
        )


LAST_RAW = ""

PROMPT = """Ты инженер, который принимает задание у заказчика.
Выпиши машинный контракт задачи: что сдать (пути и форматы), какие
ограничения жёсткие (безопасность, прочность, нормы), какие мягкие,
какие единицы, какие допущения придётся принять.

Отвечай ТОЛЬКО JSON:
{{"deliverables": [...], "hard_constraints": [...], "soft_constraints": [...],
  "units": "...", "assumptions": [...]}}

PHASE: contract

ИНСТРУКЦИЯ:
{instruction}

ФАЙЛЫ ПРОЕКТА:
{files}
"""


def extract_json(text: str) -> str:
    """Достать JSON из ответа «думающей» модели: снять <thought>, найти сбалансированный объект."""
    text = re.sub(r"<thought>.*?</thought>", "", text, flags=re.S | re.I)
    text = re.sub(r"<thinking>.*?</thinking>", "", text, flags=re.S | re.I)
    text = _strip_fences(text)
    start = text.find("{")
    if start < 0:
        return text
    depth, in_str, esc = 0, False, False
    for i in range(start, len(text)):
        ch = text[i]
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
                    return text[start:i + 1]
    return text[start:]


def _strip_fences(text: str) -> str:
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    return text


def parse(raw: str) -> Contract:
    try:
        data = json.loads(extract_json(raw))
    except json.JSONDecodeError:
        return Contract(raw={"parse_error": raw[:500]})
    return Contract(
        deliverables=[str(x) for x in data.get("deliverables", [])],
        hard=[str(x) for x in data.get("hard_constraints", [])],
        soft=[str(x) for x in data.get("soft_constraints", [])],
        units=str(data.get("units", "")),
        assumptions=[str(x) for x in data.get("assumptions", [])],
        raw=data,
    )


async def extract(model, instruction: str, files: list[str]) -> tuple[Contract, dict]:
    """Один вызов модели. Возвращает (контракт, usage)."""
    prompt = PROMPT.format(instruction=instruction.strip(), files="\n".join(files[:60]))
    text, usage = await model.complete([{"role": "user", "content": prompt}], max_tokens=2000)
    global LAST_RAW
    LAST_RAW = text
    return parse(text), usage
