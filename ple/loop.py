"""Цикл решения задачи: 7 шагов из архитектуры.

contract → recon → v0 → work → verify/repair → optimise → package

Каждый шаг пишет строку в лог прогона, поэтому после факта видно,
где именно система свернула не туда.
"""
from __future__ import annotations

import json
import os
import re

from . import contract as contract_mod
from .budget import Budget
from .protocol import Cancelled, MCPClient, Session, TaskError
from .verify import Violation, check, describe, is_clean

DEFAULT_DELIVERABLES = ["/app/out/answers.json", "/app/out/report.md"]


class Agent:
    def __init__(self, session: Session, model, budget: Budget):
        self.s = session
        self.model = model
        self.b = budget
        self.contract = None
        self.evidence: dict = {}
        self.notes: list[str] = []

    async def log(self, text: str) -> None:
        self.notes.append(text)
        await self.s.log(f"[{self.b.current}] {text}")

    # --- 1. контракт -------------------------------------------------------
    async def step_contract(self) -> None:
        self.b.begin("contract")
        listing = await self.s.exec("find . -maxdepth 3 -type f | sort | head -60", 30)
        files = [ln for ln in listing.output.splitlines() if ln.strip()]
        self.contract, usage = await contract_mod.extract(self.model, self.s.instruction, files)
        await self.s.usage_add(getattr(self.model, "name", "unknown"),
                               usage["input_tokens"], usage["output_tokens"],
                               usage.get("cache_read_tokens", 0))
        self.b.note_tokens(usage)
        if not self.contract.deliverables:
            self.contract.deliverables = list(DEFAULT_DELIVERABLES)
        if not self.contract.hard:
            await self.log(f"контракт без жёстких ограничений; сырой ответ: "
                           f"{contract_mod.LAST_RAW[:200]}")
        await self.log(f"контракт: {len(self.contract.deliverables)} деливераблов, "
                       f"{len(self.contract.hard)} жёстких ограничений")

    # --- 2. рекогносцировка ------------------------------------------------
    async def step_recon(self) -> None:
        self.b.begin("recon")
        data = ""
        for path in ("data/geometry.csv", "docs/criteria.md"):
            try:
                data += (await self.s.read_file(path)).decode("utf-8", "replace")
            except TaskError as e:
                await self.log(f"чтение {path} не удалось: {e}")
        self.evidence = self._parse_evidence(data)
        if self.s.mcp_servers:
            await self._recon_mcp()
        brief = {k: v for k, v in self.evidence.items() if k != "raw_text"}
        await self.log(f"доказательства: {brief}")

    @staticmethod
    def _parse_evidence(text: str) -> dict:
        def num(key: str, default: float) -> float:
            m = re.search(rf"{key}\s*[=:,]\s*([0-9.]+)", text)
            return float(m.group(1)) if m else default

        q, h, L = num("wind_q_kpa", 0.6), num("height_m", 6.0), num("length_m", 60.0)
        walls = int(num("walls", 2))
        return {
            "wind_q_kpa": q, "height_m": h, "length_m": L, "walls": walls,
            "capacity_a": num("capacity_a", 120.0), "capacity_b": num("capacity_b", 180.0),
            "cost_a": num("cost_a", 100.0), "cost_b": num("cost_b", 160.0),
            "expected_shear": q * h * L,
            "raw_text": text[:4000],
        }

    async def _recon_mcp(self) -> None:
        """Ленивое обнаружение: в контекст попадают 2 инструмента из 40."""
        server = self.s.mcp_servers[0]
        client = MCPClient(self.s, server["url"])
        try:
            await client.initialize()
            tools = await client.list_tools()
        except TaskError as e:
            await self.log(f"MCP недоступен: {e}")
            return
        index = [{"name": t.get("name"), "desc": (t.get("description") or "")[:120]}
                 for t in tools]
        wanted = [t for t in index
                  if any(k in f"{t['name']} {t['desc']}".lower()
                         for k in ("wind", "criteria", "criter"))][:2]
        await self.log(f"MCP: каталог {len(tools)} инструментов → выбрано "
                       f"{[t['name'] for t in wanted]}")
        for t in wanted:
            try:
                res = await client.call_tool(t["name"], {"name": "wind"})
                text = json.dumps(res, ensure_ascii=False)
                self.evidence.update(self._parse_evidence(text))
            except TaskError as e:
                await self.log(f"MCP call {t['name']} failed: {e}")

    # --- 3. черновик v0 ----------------------------------------------------
    async def step_v0(self) -> None:
        self.b.begin("v0")
        stub = {"diaphragm_shear_kN": 0.0, "wall_load_kN": 0.0, "unit": "kN",
                "wall_option": "B", "walls_served": self.evidence.get("walls", 2),
                "code_reference": "", "assumption": ""}
        report = ("# Черновик\n\n## Расчёт\nв работе\n\n## Ограничения\nв работе\n\n"
                  "## Ссылки на нормы\nв работе\n")
        for path, body in zip(self._deliverables(), (stub, report)):
            payload = json.dumps(body, ensure_ascii=False, indent=2) if isinstance(body, dict) else body
            await self.s.write_file(path, payload)
        await self.log("v0 записан: файл на диске уже есть")

    def _evidence_block(self) -> str:
        """Доказательства в чистом виде: «ключ = значение», без вложенного JSON.

        Модели плохо извлекают числа из вложенных JSON-строк (проверено на YandexGPT:
        получала {"content":[{"text":"wind_q_kpa=0.6;..."}]} и возвращала null).
        """
        pairs = [f"{k} = {v}" for k, v in self.evidence.items() if k != "raw_text"]
        if not pairs:
            return self.evidence.get("raw_text", "данных нет")
        block = "\n".join(pairs)
        raw = self.evidence.get("raw_text", "")
        if raw and len(raw) < 600:
            block += "\n\nСЫРЫЕ ДАННЫЕ:\n" + raw
        return block

    def _deliverables(self) -> tuple[str, str]:
        d = self.contract.deliverables if self.contract else []
        json_path = next((p for p in d if p.endswith(".json")), DEFAULT_DELIVERABLES[0])
        md_path = next((p for p in d if p.endswith(".md")), DEFAULT_DELIVERABLES[1])
        return json_path, md_path

    # --- 4. работа ---------------------------------------------------------
    ANSWER_SKELETON = (
        '{\n'
        '  "diaphragm_shear_kN": <число>,\n'
        '  "wall_load_kN": <число>,\n'
        '  "unit": "kN",\n'
        '  "wall_option": "A",\n'
        '  "walls_served": <целое число стен>,\n'
        '  "code_reference": "<норма>",\n'
        '  "assumption": "<одно предложение>"\n'
        '}'
    )

    PHASE_HINT = {
        "answers": "Определи все величины из исходных данных и заполни JSON.",
        "repair": "Исправь перечисленные нарушения и верни ПОЛНЫЙ JSON заново.",
        "optimise": ("Все жёсткие ограничения уже выполнены. Выбери вариант стены "
                     "с НАИМЕНЬШЕЙ стоимостью среди тех, что проходят по прочности, "
                     "и верни полный JSON."),
    }

    async def step_work(self, phase: str = "answers", hint: str = "") -> dict:
        if self.b.over():
            await self.log("потолок расхода: вызов модели пропущен")
            return {}
        self.b.begin("work")
        if not self.evidence:
            await self.step_recon()
        prompt = (f"{self.contract.as_context()}\n"
                  f"ИСХОДНЫЕ ДАННЫЕ:\n{self._evidence_block()}\n\n"
                  f"PHASE: {phase}\n"
                  f"{self.PHASE_HINT.get(phase, '')}"
                  + (f"\nНАРУШЕНИЯ ДЛЯ ИСПРАВЛЕНИЯ: {hint}" if hint else "")
                  + "\nВерни ТОЛЬКО JSON такой формы, без текста вокруг:\n"
                  + self.ANSWER_SKELETON)
        text, usage = await self.model.complete([{"role": "user", "content": prompt}],
                                                max_tokens=6000)
        await self.s.usage_add(getattr(self.model, "name", "unknown"),
                               usage["input_tokens"], usage["output_tokens"],
                               usage.get("cache_read_tokens", 0))
        self.b.note_tokens(usage)
        try:
            answers = json.loads(contract_mod.extract_json(text))
        except json.JSONDecodeError:
            await self.log(f"модель вернула не-JSON, оставляем v0; сырой ответ: "
                           f"{text[:240]!r}")
            answers = {}
        return answers

    async def step_report(self, answers: dict | None = None) -> None:
        prompt = (f"{self.contract.as_context()}\n"
                  f"ИСХОДНЫЕ ДАННЫЕ:\n{self._evidence_block()}\n\n"
                  "PHASE: report\n"
                  + (f"ЧИСЛА РАСЧЁТА:\n{json.dumps(answers, ensure_ascii=False)}\n" if answers else "")
                  + "Напиши отчёт на русском. Обязательные заголовки, ровно такие:\n"
                    "## Расчёт\n## Ограничения\n## Ссылки на нормы\n## Допущения\n"
                    "В разделе «Ссылки на нормы» укажи ASCE 7-22.")
        text, usage = await self.model.complete([{"role": "user", "content": prompt}],
                                                max_tokens=4000)
        await self.s.usage_add(getattr(self.model, "name", "unknown"),
                               usage["input_tokens"], usage["output_tokens"],
                               usage.get("cache_read_tokens", 0))
        self.b.note_tokens(usage)
        await self.s.write_file(self._deliverables()[1], text)

    # --- 5. проверка и ремонт ---------------------------------------------
    async def step_verify(self, answers: dict, max_rounds: int = 3) -> tuple[dict, list[Violation]]:
        self.b.begin("verify")
        violations: list[Violation] = []
        for rnd in range(1, max_rounds + 1):
            try:
                report = (await self.s.read_file(self._deliverables()[1])).decode("utf-8", "replace")
            except TaskError:
                report = ""
            violations = check(answers, report, self.evidence)
            await self.log(f"проверка #{rnd}: нарушений {len(violations)} "
                           f"({describe(violations) or 'чисто'})")
            if is_clean(violations) or self.b.over("verify"):
                break
            hard = [v for v in violations if v.hard]
            if hard:
                answers = await self.step_work("repair", hint=describe(hard))
                self.b.begin("verify")
            elif violations:
                # мягкие нарушения дешёвы в исправлении и стоят части p
                await self.step_report(answers)
                self.b.begin("verify")
                await self.s.write_file(self._deliverables()[0],
                                        json.dumps(answers, ensure_ascii=False, indent=2))
        return answers, violations

    # --- 6. оптимизация ----------------------------------------------------
    async def step_optimise(self, answers: dict, violations: list[Violation]) -> dict:
        if not is_clean(violations):
            await self.log("оптимизация пропущена: G != 1")
            return answers
        if os.environ.get("PLE_NO_OPT") == "1":
            await self.log("оптимизация отключена флагом PLE_NO_OPT")
            return answers
        if self.b.over():
            await self.log("оптимизация пропущена: потолок расхода")
            return answers
        self.b.begin("optimise")
        before = dict(answers)
        hint = (f"Сейчас выбрано: вариант {answers.get('wall_option')}, "
                f"нагрузка на стену {answers.get('wall_load_kN')} кН, "
                f"усилие {answers.get('diaphragm_shear_kN')} кН. "
                "При смене варианта стены расчётные нагрузки НЕ меняются — "
                "меняется только поле wall_option.")
        better = await self.step_work("optimise", hint=hint)
        self.b.begin("optimise")
        if better and better.get("wall_option") != answers.get("wall_option"):
            # кандидат обязан остаться в допустимой области: проверяем ДО записи
            try:
                report = (await self.s.read_file(self._deliverables()[1])).decode("utf-8", "replace")
            except TaskError:
                report = ""
            candidate_violations = [v for v in check(better, report, self.evidence) if v.hard]
            if candidate_violations:
                await self.log(f"оптимизация откатана: {describe(candidate_violations)}")
                return before
            answers = better
            await self.s.write_file(self._deliverables()[0],
                                    json.dumps(answers, ensure_ascii=False, indent=2))
            await self.log(f"оптимизация: вариант {answers.get('wall_option')} (дешевле)")
        return answers

    # --- 7. упаковка -------------------------------------------------------
    async def step_package(self, answers: dict, violations: list[Violation]) -> str:
        self.b.begin("package")
        # перепроверяемся по фактическим файлам — но только если соединение живо:
        # после cancel любой запрос падает, и упаковка обязана быть локальной
        if not self.s.cancelled:
            try:
                report = (await self.s.read_file(self._deliverables()[1])).decode("utf-8", "replace")
                violations = check(answers, report, self.evidence)
            except (TaskError, Cancelled):
                pass
        if not answers:
            status = "черновик: работа не начата"
        elif is_clean(violations):
            status = "все жёсткие ограничения выполнены"
        else:
            status = f"ЕСТЬ НАРУШЕНИЯ: {describe([v for v in violations if v.hard])}"
        json_path, md_path = self._deliverables()
        return (
            f"Сдано: {json_path} (усилие {answers.get('diaphragm_shear_kN')} кН, "
            f"нагрузка на стену {answers.get('wall_load_kN')} кН, вариант "
            f"{answers.get('wall_option')}), {md_path} (отчёт). {status}."
        )


async def run(session: Session, model, budget: Budget) -> None:
    agent = Agent(session, model, budget)
    answers: dict = {}
    violations: list[Violation] = []
    final = "Ничего не сдано: задача прервана до первого файла."
    try:
        await agent.step_contract()
        # губернатор реально гейтит шаги: v0 пишем всегда, остальное — по остатку бюджета
        await agent.step_recon() if not budget.over() else await agent.log("recon пропущен: бюджет исчерпан")
        await agent.step_v0()
        answers = await agent.step_work() if not budget.over() else {}
        if answers:
            await session.write_file(agent._deliverables()[0],
                                     json.dumps(answers, ensure_ascii=False, indent=2))
        if not budget.over():
            await agent.step_report(answers)
        answers, violations = await agent.step_verify(answers)
        answers = await agent.step_optimise(answers, violations)
        final = await agent.step_package(answers, violations)
    except Cancelled:
        try:
            final = "Время вышло. " + await agent.step_package(answers, violations)
        except Exception:  # noqa: BLE001 — упаковка не имеет права уронить задачу
            final = "Время вышло; файлы, записанные до отмены, сохранены."
    except TaskError as e:
        final = f"Ошибка инструмента: {e}. " + await agent.step_package(answers, violations)
    except Exception as e:  # noqa: BLE001 — харнесс не должен падать молча
        final = f"Внутренняя ошибка харнесса: {e!r}."
    finally:
        await session.done(final)
