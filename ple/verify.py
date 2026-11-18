"""Верификатор: программные проверки, независимые от того, как считали ответ.

Ключевая идея — проверка не повторяет рассуждение решателя, а пересчитывает
от исходных данных. Именно это превращает «91 % ограничений» в «G = 1».
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Violation:
    code: str
    message: str
    hard: bool
    target: str  # что маскировать при локальном ремонте


def _num(value, default: float = 0.0) -> float:
    """Терпимое приведение к числу: модель может вернуть null, строку или «108 кН»."""
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).replace(",", ".").split()[0])
    except (ValueError, IndexError):
        return default


def _near(a: float, b: float, rel: float = 0.01) -> bool:
    if b == 0:
        return abs(a) < 1e-9
    return abs(a - b) / abs(b) <= rel


def check(answers: dict, report: str, evidence: dict) -> list[Violation]:
    v: list[Violation] = []
    shear = evidence["expected_shear"]
    walls = evidence["walls"]
    option = str(answers.get("wall_option", "")).upper()
    capacity = {"A": evidence["capacity_a"], "B": evidence["capacity_b"]}.get(option)

    # --- жёсткие ----------------------------------------------------------
    if not _near(_num(answers.get("diaphragm_shear_kN")), shear):
        v.append(Violation("H1", f"диафрагменное усилие {answers.get('diaphragm_shear_kN')} "
                                 f"не равно расчётному {shear:.1f} кН", True, "shear"))
    if str(answers.get("unit", "")) != "kN":
        v.append(Violation("H2", f"единицы {answers.get('unit')!r}, требуется kN", True, "unit"))
    if capacity is None:
        v.append(Violation("H3", f"неизвестный вариант стены {option!r}", True, "wall_option"))
    else:
        load = _num(answers.get("wall_load_kN"))
        if load > capacity:
            v.append(Violation("H4", f"нагрузка на стену {load:.1f} кН > "
                                     f"несущей способности {capacity:.1f} кН", True, "wall_load"))
    if int(_num(answers.get("walls_served"))) != walls:
        v.append(Violation("H5", f"обслужено {answers.get('walls_served')} стен из {walls}",
                           True, "walls_served"))
    load = _num(answers.get("wall_load_kN"))
    if not _near(load, shear / walls):
        v.append(Violation("H6", f"нагрузка {load} не равна F/{walls} = {shear / walls:.1f}",
                           True, "wall_load"))

    # --- мягкие -----------------------------------------------------------
    for section in ("Расчёт", "Ограничения", "Ссылки на нормы"):
        if section.lower() not in report.lower():
            v.append(Violation("S1", f"в отчёте нет раздела «{section}»", False, "report"))
    if "asce" not in report.lower():
        v.append(Violation("S2", "в отчёте нет ссылки на ASCE 7", False, "report"))
    if not str(answers.get("assumption", "")).strip():
        v.append(Violation("S3", "не записано допущение", False, "assumption"))
    return v


def is_clean(violations: list[Violation]) -> bool:
    return not any(x.hard for x in violations)


def describe(violations: list[Violation]) -> str:
    return "; ".join(f"{x.code}({'H' if x.hard else 'S'}): {x.message}" for x in violations)
