"""Генераторы отчётов. Новый формат = новая функция + запись в REPORTERS.

Тип ReconcileResult импортируется только для подсказок (TYPE_CHECKING),
чтобы избежать циклического импорта billing_check <-> reporting.
"""
from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Callable

import pandas as pd

import config

if TYPE_CHECKING:
    from billing_check import ReconcileResult

Writer = Callable[["ReconcileResult", Path], None]

ERROR_LABELS: dict[str, str] = {
    "A_offpeak_in_peak": "A: весь запрос по off-peak в peak-окне",
    "B_reasoning_peak_in_offpeak": "B: reasoning по peak в off-peak окне",
    "C_reasoning_doubled": "C: reasoning ×2 (модель с одной ценой)",
    "peak_in_offpeak": "Весь запрос по peak в off-peak окне",
    "not_in_pricelist": "Модель вне прайса, но списана",
    "broken_total": "total ≠ сумма компонент",
    "unclassified": "Не классифицировано (ручной разбор)",
}


def _fmt(x: Decimal, places: int = 6) -> str:
    return f"{x:.{places}f}"


def write_summary_md(result: ReconcileResult, path: Path) -> None:
    t = result.totals
    lines: list[str] = [
        "# Сверка биллинга DeepSeek · 13–14.10.2026 (UTC)",
        "",
        f"- Задание: [{config.TASK_DOC_URL}]({config.TASK_DOC_URL})",
        f"- Данные: {result.source}",
        f"- Сформировано: {result.generated_at:%Y-%m-%d %H:%M:%S} UTC",
        "- Запуск: `python billing_check.py` (или `--file path.xlsx`)",
        "- SQL-вариант решения: [sql_solution.md](../sql_solution.md), демо: `python sql_demo.py`",
        "",
        "## 1. Итоги",
        "",
        "| Метрика | USD |",
        "|---|---:|",
        f"| Корректная стоимость (эталон) | {_fmt(t['correct'])} |",
        f"| Сумма выгрузки | {_fmt(t['export'])} |",
        f"| **Разница (выгрузка − эталон)** | **{_fmt(t['delta'])}** |",
        "",
        f"Строк: {t['rows']:.0f} · значимых расхождений: {t['discrepancies']:.0f}.",
        "Знак «−» = выгрузка недосписывает (недополученная выручка).",
        "",
        "> Выборка «до 4500 строк на пару клиент+модель, равномерно по суткам» не "
        "репрезентативна для экстраполяции потерь на весь трафик — только для обнаружения типов ошибок.",
        "",
        "## 2. Ошибки по типам",
        "",
        "| Тип | Запросов | Влияние, USD |",
        "|---|---:|---:|",
    ]
    for r in result.error_stats.itertuples(index=False):
        lines.append(f"| {ERROR_LABELS.get(r.error_type, r.error_type)} | {r.count} | {_fmt(r.impact)} |")

    lines += ["", "## 3. Подтверждения на request_id", ""]
    for kind, rows in result.examples.items():
        lines.append(f"### {ERROR_LABELS.get(kind, kind)}")
        for r in rows:
            lines.append(
                f"- `{r['request_id']}` ({r['client']}, {r['model_group']}, "
                f"{r['start_time']:%m-%d %H:%M:%S} UTC, окно {r['window']}): "
                f"выгрузка {_fmt(r['export_total'], 8)} vs эталон {_fmt(r['correct_total'], 8)}, "
                f"Δ {_fmt(r['delta'], 8)}. {r['evidence']}"
            )
        lines.append("")

    lines += ["## 4. Проверки данных", "", "| Проверка | Статус | Детали |", "|---|---|---|"]
    for c in result.checks:
        lines.append(f"| {c.name} | {'OK' if c.passed else 'FAIL'} | {c.detail} |")

    lines += [
        "",
        "## 5. Нюансы методологии",
        "",
        "- **Формула text_tokens:** лист пишет `completion − reasoning − audio − image − video`, "
        "задание — `− audio_input_tokens − image_tokens − output_image_tokens`. Колонки `video` нет; "
        "использована формула задания (нормативная). Расхождение — вопрос владельцу данных.",
        "- **Проза vs таблица:** «базовая цена = peak, окна = off-peak» не выполняется буквально "
        "(0731 t00-01 = peak; ali выходные = peak). Ставки взяты из строк таблицы. "
        "Гипотеза первопричины типа A: баг-версия применила единый шаблон «ночь = off-peak» ко всем моделям.",
        "- Деньги: Decimal, округление только при выводе (ROUND_HALF_UP до 8 знаков как в выгрузке).",
        "",
        "## 6. Причины расхождения и запрашиваемые данные",
        "",
        "| Гипотеза | Что запросить |",
        "|---|---|",
        "| Backfill/replay со старой версией прайса | timeline инцидента, логи backfill, changelog прайсов, created_at/updated_at строк |",
        "| Единый шаблон расписания вместо per-model (тип A) | версия кода тарификации; проверить кластеризацию ошибок A по окнам t00-01/t04-06/t10-14 |",
        "| Stale cache конфига на инстансах | instance_id, версия прайса на инстансе, логи деплоя |",
        "| Задвоение компонента (тип C) | миграции поля reasoning, unique-констрейнт по request_id, логи retry |",
        "| Дубли/пропуски между источниками | построчная выгрузка контрольного источника, count(*) по часам×org×model из обоих |",
        "| Разница политик округления | precision/rounding mode хранения cost в обоих источниках |",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")


def write_discrepancies_csv(result: ReconcileResult, path: Path) -> None:
    df = result.discrepancies.copy()
    df["start_time"] = df["start_time"].dt.strftime("%Y-%m-%d %H:%M:%S.%f")
    for c in ("export_total", "correct_total", "delta"):
        df[c] = df[c].map(lambda x: f"{x:.8f}")
    df.to_csv(path, index=False, encoding="utf-8-sig")


def write_result_xlsx(result: ReconcileResult, path: Path) -> None:
    summary = pd.DataFrame([
        {"Метрика": "Корректная стоимость (эталон), USD", "Значение": float(result.totals["correct"])},
        {"Метрика": "Сумма выгрузки, USD", "Значение": float(result.totals["export"])},
        {"Метрика": "Разница (выгрузка − эталон), USD", "Значение": float(result.totals["delta"])},
        {"Метрика": "Строк", "Значение": int(result.totals["rows"])},
        {"Метрика": "Значимых расхождений", "Значение": int(result.totals["discrepancies"])},
    ])
    disc = result.discrepancies.copy()
    disc["start_time"] = disc["start_time"].dt.strftime("%Y-%m-%d %H:%M:%S.%f")
    for c in ("export_total", "correct_total", "delta"):
        disc[c] = disc[c].map(float)
    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        summary.to_excel(writer, sheet_name="Итоги", index=False)
        result.error_stats.to_excel(writer, sheet_name="Ошибки", index=False)
        disc.to_excel(writer, sheet_name="Расхождения", index=False)


# Реестр: добавить новый формат = написать функцию + зарегистрировать здесь
REPORTERS: dict[str, Writer] = {
    "summary.md": write_summary_md,
    "discrepancies.csv": write_discrepancies_csv,
    "result.xlsx": write_result_xlsx,
}