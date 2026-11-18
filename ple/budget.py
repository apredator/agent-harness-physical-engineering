"""Бюджетный губернатор.

Задача живёт 6 часов, но убивает её не время само по себе, а привычка
работать до звонка. Губернатор делит бюджет на фазы и умеет принудительно
переводить в упаковку — это защита от 4.3 % провалов «таймаут/ошибка».
"""
from __future__ import annotations

import os
import time


class Budget:
    # доли от общего времени: v0 пишется рано, оптимизация — только в конце
    PHASES = {
        "contract": 0.08,
        "recon": 0.12,
        "v0": 0.10,        # черновик всех деливераблов
        "work": 0.35,
        "verify": 0.20,
        "optimise": 0.10,
        "package": 0.05,
    }

    def __init__(self, total_seconds: float | None, token_cap: int | None = None):
        self.total = total_seconds
        # жёсткий потолок расхода: защита денег, а не времени
        self.token_cap = token_cap if token_cap is not None else (
            int(os.environ["PLE_TOKEN_CAP"]) if os.environ.get("PLE_TOKEN_CAP") else None)
        self.token_exceeded = False
        self.started = time.monotonic()
        self.spent = 0.0
        self.current = "contract"
        self.deadlines: dict[str, float] = {}
        if total_seconds:
            cursor = self.started
            for name, share in self.PHASES.items():
                cursor += total_seconds * share
                self.deadlines[name] = cursor

    # --- время -------------------------------------------------------------
    def elapsed(self) -> float:
        return time.monotonic() - self.started

    def remaining(self) -> float | None:
        if self.total is None:
            return None
        return max(0.0, self.total - self.elapsed())

    def begin(self, phase: str) -> None:
        self.current = phase

    def over(self, phase: str | None = None) -> bool:
        """Пора заканчивать: кончилось время задачи, токены, или (для фазы) её окно.

        Без аргумента — только общий дедлайн: дедлайн фазы не должен обрывать
        весь прогон, иначе медленный первый вызов убивает задачу.
        """
        if self.token_exceeded:
            return True
        if self.total is None:
            return False
        now = time.monotonic()
        if now > self.started + self.total * 0.95:
            return True
        if phase is None:
            return False
        deadline = self.deadlines.get(phase)
        return deadline is not None and now > deadline

    def allows(self, seconds: float) -> bool:
        """Хватит ли времени на ещё один шаг."""
        left = self.remaining()
        return True if left is None else left > seconds

    # --- токены ------------------------------------------------------------
    def note_tokens(self, usage: dict) -> None:
        self.spent += usage.get("input_tokens", 0) + usage.get("output_tokens", 0)
        if self.token_cap and self.spent > self.token_cap:
            self.token_exceeded = True

    def summary(self) -> str:
        left = self.remaining()
        return (f"budget: elapsed={self.elapsed():.0f}s "
                f"left={'n/a' if left is None else f'{left:.0f}s'} "
                f"tokens≈{self.spent}"
                + (f"/cap {self.token_cap}" if self.token_cap else "")
                + (" ПРЕВЫШЕН" if self.token_exceeded else ""))
