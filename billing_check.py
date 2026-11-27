"""Сверка биллинга DeepSeek: эталонный расчёт по прайсу и поиск расхождений.

Деньги — Decimal (точная арифметика, воспроизведение округления выгрузки
ROUND_HALF_UP до 8 знаков). pandas — только загрузка/группировки.
"""
from __future__ import annotations

import argparse
import io
import re
import sys
import time
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any

import pandas as pd

import config
import reporting

D = Decimal
MILLION = D("1000000")
Q8 = config.ROUND_SCALE
# Компоненты выгрузки хранятся с полной точностью (9–10 знаков), total — 8.
# Сравниваем компоненты с НЕокруглённым эталоном, допуск 5e-9 покрывает оба режима.
COMP_TOL = D("0.000000005")

TOKEN_COLS: tuple[str, ...] = (
    "prompt_tokens", "completion_tokens", "cached_input_tokens",
    "reasoning_tokens", "accepted_prediction_tokens", "rejected_prediction_tokens",
    "image_tokens", "output_image_tokens", "audio_input_tokens", "audio_output_tokens",
    "cache_creation_tokens", "total_tokens",
)
# cost-компоненты выгрузки (порядок = порядок в total)
COST_COLS: tuple[str, ...] = (
    "input_cost", "cached_input_cost", "output_cost", "reasoning_cost",
    "prediction_cost", "image_cost", "cache_creation_cost", "web_search_cost",
)
# компоненты, пересчитываемые по прайсу (остальные берём из выгрузки)
REF_COLS: tuple[str, ...] = COST_COLS[:6]

DAY_NUM: dict[str, int] = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}


def _max_dev(exp: dict[str, Decimal], ref: dict[str, Decimal]) -> Decimal:
    return max((abs(exp[c] - ref[c]) for c in REF_COLS), default=D(0))

def q8(x: Decimal) -> Decimal:
    """Округление как в выгрузке: half-up до 8 знаков."""
    return x.quantize(Q8, rounding=ROUND_HALF_UP)


@dataclass(frozen=True, slots=True)
class Rate:
    input: Decimal          # ставка за 1 токен
    output: Decimal
    cache_read: Decimal
    label: str              # peak / off-peak
    window: str


@dataclass(frozen=True, slots=True)
class Window:
    weekdays: frozenset[int]
    h_from: int             # включительно
    h_to: int               # исключено
    rate: Rate


@dataclass(slots=True)
class CheckResult:
    name: str
    passed: bool
    detail: str
    affected: int = 0


@dataclass(slots=True)
class ReconcileResult:
    source: str
    generated_at: datetime
    logs: pd.DataFrame
    discrepancies: pd.DataFrame
    checks: list[CheckResult]
    totals: dict[str, Decimal]
    error_stats: pd.DataFrame
    examples: dict[str, list[dict[str, Any]]] = field(default_factory=dict)


# ---------------------------------------------------------------- загрузка
def _to_export_url(url: str) -> str:
    """Любую ссылку на Google-таблицу приводим к export?format=xlsx (оба листа разом)."""
    if "/export" in url:
        return url
    m = re.search(r"/spreadsheets/d/([\w-]+)", url)
    if m:
        return f"https://docs.google.com/spreadsheets/d/{m.group(1)}/export?format=xlsx"
    return url


def load_workbook(source: str) -> pd.ExcelFile:
    """source: URL Google-таблицы или локальный путь .xlsx — парсер один и тот же."""
    if source.startswith(("http://", "https://")):
        url = _to_export_url(source)
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                data = resp.read()
        except Exception as exc:
            raise RuntimeError(f"Не удалось скачать таблицу {url}: {exc}") from exc
        return pd.ExcelFile(io.BytesIO(data))
    path = Path(source)
    if not path.exists():
        raise FileNotFoundError(f"Файл не найден: {path}")
    return pd.ExcelFile(path)


def _to_int(v: Any) -> int:
    """Токены: '1,020' / 1020.0 / '' / 'nan' -> int."""
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return 0
    s = str(v).replace(",", "").replace(" ", "").strip()
    if not s or s.lower() in ("nan", "none"):
        return 0
    return int(float(s))


def _to_dec(v: Any) -> Decimal:
    """Деньги/ставки: запятая как десятичный разделитель тоже допускается; 'nan' -> 0."""
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return D(0)
    s = str(v).strip().replace(" ", "").replace(",", ".")
    if not s or s.lower() in ("nan", "none"):
        return D(0)
    return D(s)


def _find_header(raw: pd.DataFrame, marker: str) -> int:
    for i, row in raw.iterrows():
        if any(marker in str(c) for c in row.values):
            return int(i)
    raise ValueError(f"Не найдена строка заголовка с '{marker}'")


def parse_logs(xls: pd.ExcelFile) -> pd.DataFrame:
    raw = pd.read_excel(xls, sheet_name=config.SHEET_LOGS, header=None, dtype=object)
    hdr = _find_header(raw, "request_id")
    cols = [str(c).strip() for c in raw.iloc[hdr].values]
    df = raw.iloc[hdr + 1:].copy()
    df.columns = cols
    df = df.rename(columns={"start_time (UTC)": "start_time", "Клиент": "client"})
    df = df.dropna(subset=["request_id"]).reset_index(drop=True)

    missing = [c for c in (*TOKEN_COLS, *COST_COLS, "total_cost") if c not in df.columns]
    if missing:
        _log(f"WARN: отсутствуют колонки {missing} — считаю 0")

    # В полном файле формат смешанный: "…07:24:53" и "…07:24:53.123456".
    # ISO8601 парсит оба; astype("string") нормализует и Excel-datetime тоже.
    df["start_time"] = pd.to_datetime(
        df["start_time"].astype("string").str.strip(), format="ISO8601", utc=True
    )
    for c in TOKEN_COLS:
        df[c] = df[c].map(_to_int) if c in df.columns else 0
    for c in (*COST_COLS, "total_cost"):
        df[c] = df[c].map(_to_dec) if c in df.columns else D(0)
    for c in ("request_id", "organization_id", "model_group", "client", "status"):
        df[c] = df[c].astype(str).str.strip() if c in df.columns else ""
    return df


def parse_prices(xls: pd.ExcelFile) -> tuple[dict[str, str], pd.DataFrame]:
    """Возвращает (org_id -> прайс-лист, таблица ставок)."""
    raw = pd.read_excel(xls, sheet_name=config.SHEET_PRICES, header=None, dtype=object)

    # --- секция 1: организации -> прайс-лист
    org_hdr = _find_header(raw, "id организации")
    org_cols = [str(c).strip() for c in raw.iloc[org_hdr].values]
    i_id, i_list = org_cols.index("id организации"), org_cols.index("Прайс-лист")
    org_map: dict[str, str] = {}
    for _, row in raw.iloc[org_hdr + 1:].iterrows():
        if pd.isna(row.iloc[i_id]):
            break
        oid, plist = str(row.iloc[i_id]).strip(), str(row.iloc[i_list]).strip()
        if not re.fullmatch(r"[\w-]{20,}", oid):
            break
        org_map[oid] = plist

    # --- секция 2: ставки
    p_hdr = _find_header(raw, "Окно (UTC)")
    p_cols = [str(c).strip() for c in raw.iloc[p_hdr].values]
    idx = {name: p_cols.index(name) for name in
           ("Прайс-лист", "Модель", "Окно (UTC)", "Дни", "Peak или off-peak",
            "Input $/1M", "Output $/1M", "Cache read $/1M")}
    rows: list[dict[str, Any]] = []
    for _, row in raw.iloc[p_hdr + 1:].iterrows():
        raw_model = row.iloc[idx["Модель"]]
        if pd.isna(raw_model):
            break
        model = str(raw_model).strip()
        if not model or model.startswith("3."):
            break
        rows.append({
            "price_list": str(row.iloc[idx["Прайс-лист"]]).strip(),
            "model_group": model,
            "window": str(row.iloc[idx["Окно (UTC)"]]).strip(),
            "days": str(row.iloc[idx["Дни"]]).strip().lower(),
            "label": str(row.iloc[idx["Peak или off-peak"]]).strip().lower(),
            "input_1m": _to_dec(row.iloc[idx["Input $/1M"]]),
            "output_1m": _to_dec(row.iloc[idx["Output $/1M"]]),
            "cache_1m": _to_dec(row.iloc[idx["Cache read $/1M"]]),
        })
    return org_map, pd.DataFrame(rows)


# ---------------------------------------------------------------- расписание
def build_schedule(prices: pd.DataFrame) -> dict[tuple[str, str], list[Window]]:
    sched: dict[tuple[str, str], list[Window]] = {}
    for r in prices.itertuples(index=False):
        days = _parse_days(r.days)
        h_from, h_to = _parse_window(r.window)
        rate = Rate(r.input_1m / MILLION, r.output_1m / MILLION, r.cache_1m / MILLION,
                    r.label, r.window)
        sched.setdefault((r.price_list, r.model_group), []).append(
            Window(frozenset(days), h_from, h_to, rate))
    _validate_coverage(sched)
    return sched


def _parse_days(days: str) -> list[int]:
    if "ежедневно" in days:
        return list(range(7))
    return [DAY_NUM[d.strip()] for d in days.split(",") if d.strip() in DAY_NUM]


def _parse_window(window: str) -> tuple[int, int]:
    w = window.lower()
    if "одна цена" in w or "все сутки" in w:
        return 0, 24
    m = re.fullmatch(r"[tw](\d{2})-(\d{2})", w)
    if not m:
        raise ValueError(f"Неизвестный формат окна: {window}")
    return int(m.group(1)), int(m.group(2))


def _validate_coverage(sched: dict[tuple[str, str], list[Window]]) -> None:
    """Самопроверка прайса: окна каждой модели покрывают 168 ч без пересечений."""
    for key, windows in sched.items():
        for wd in range(7):
            spans = sorted((w.h_from, w.h_to) for w in windows if wd in w.weekdays)
            cursor = 0
            for a, b in spans:
                if a != cursor:
                    raise ValueError(f"Расписание {key}: разрыв/пересечение в день {wd} на {a}")
                cursor = b
            if cursor != 24:
                raise ValueError(f"Расписание {key}: день {wd} покрыт до {cursor}, не до 24")


def resolve_rate(org_id: str, model: str, dt: datetime,
                 org_map: dict[str, str],
                 sched: dict[tuple[str, str], list[Window]]) -> Rate | None:
    """None = модели нет в прайсе организации -> списание в ноль (правило задания)."""
    plist = org_map.get(org_id)
    windows = sched.get((plist, model)) if plist else None
    if not windows:
        return None
    wd, hour = dt.weekday(), dt.hour
    matched = [w.rate for w in windows if wd in w.weekdays and w.h_from <= hour < w.h_to]
    if len(matched) != 1:
        raise ValueError(f"Ожидалось ровно 1 окно для {org_id}/{model} @ {dt}, got {len(matched)}")
    return matched[0]


# ---------------------------------------------------------------- расчёт
def ref_components(row: Any, rate: Rate | None) -> dict[str, Decimal]:
    """Эталонные компоненты строго по формулам задания.

    НЮАНС: лист пишет text_tokens = completion - reasoning - audio - image - video,
    задание — completion - reasoning - audio_input_tokens - image_tokens - output_image_tokens.
    Колонки video нет; берём формулу ЗАДАНИЯ (нормативная, бьётся с колонками).
    """
    if rate is None:
        return {c: D(0) for c in REF_COLS}
    text = (row.completion_tokens - row.reasoning_tokens - row.audio_input_tokens
            - row.image_tokens - row.output_image_tokens)
    return {
        "input_cost": (row.prompt_tokens - row.cached_input_tokens) * rate.input,
        "cached_input_cost": row.cached_input_tokens * rate.cache_read,
        "output_cost": text * rate.output,
        "reasoning_cost": row.reasoning_tokens * rate.output,
        "prediction_cost": (row.accepted_prediction_tokens + row.rejected_prediction_tokens) * rate.output,
        "image_cost": row.image_tokens * rate.input,
    }


def _total(ref: dict[str, Decimal], row: Any) -> Decimal:
    # cache_creation_cost и web_search_cost берём из выгрузки как сказано в задании
    return sum(ref.values(), D(0)) + row.cache_creation_cost + row.web_search_cost


def classify(row: Any, ref: dict[str, Decimal], rate: Rate | None,
             alt_rates: list[Rate]) -> tuple[str, str]:
    exp = {c: getattr(row, c) for c in COST_COLS}
    ref_total = _total(ref, row)
    comp_sum = sum(exp.values(), D(0))
    dev = _max_dev(exp, ref)

    # 1. структурное повреждение total (всегда материально)
    if abs(comp_sum - row.total_cost) > Q8 * 2:
        if rate is not None and dev <= COMP_TOL:
            if row.total_cost == 0:
                return "total_zeroed", f"компоненты корректны (dev {dev}), total обнулён, Σ={comp_sum}"
            return "broken_total", f"компоненты корректны, total={row.total_cost} vs Σ={comp_sum}"
        return "broken_total", f"total={row.total_cost} != Σ={comp_sum}"

    # 2. модель вне прайса
    if rate is None:
        if any(exp[c] > 0 for c in REF_COLS):
            return "not_in_pricelist", "модель вне прайса, но списание > 0"
        return "ok", ""

    # 3. точное совпадение компонент; total — в пределах округления до 8
    if dev <= COMP_TOL and abs(ref_total - row.total_cost) <= Q8 * 2:
        return "ok", ""

    # 4. порог значимости ДО сигнатур (как WHERE в SQL) — иначе sub-1e-6 строки раздувают material
    if abs(row.total_cost - ref_total) <= config.MATERIAL_DELTA:
        return "rounding_noise", "ниже порога значимости 1e-6"

    # 5. весь запрос воспроизводится альтернативной ставкой (точное совпадение компонент)
    for alt in alt_rates:
        if alt == rate:
            continue
        if _max_dev(exp, ref_components(row, alt)) <= COMP_TOL:
            kind = "A_offpeak_in_peak" if alt.input < rate.input else "peak_in_offpeak"
            return kind, f"воспроизводится ставкой {alt.label} ({alt.window})"

    # 6. только reasoning ×2 (соседние компоненты точны)
    others_ok = all(abs(exp[c] - ref[c]) <= COMP_TOL for c in REF_COLS if c != "reasoning_cost")
    if others_ok and abs(exp["reasoning_cost"] - ref["reasoning_cost"] * 2) <= COMP_TOL:
        if len({(r.input, r.output, r.cache_read) for r in alt_rates}) > 1:
            return "B_reasoning_peak_in_offpeak", "reasoning ×2 (peak-ставка в off-peak окне)"
        return "C_reasoning_doubled", "reasoning ×2 (модель с одной ценой)"

    # 7. ratio-сигнатура (fallback)
    bad = [c for c in REF_COLS if ref[c] > 0 and abs(exp[c] - ref[c]) > COMP_TOL]
    if bad:
        ratios = [exp[c] / ref[c] for c in bad]
        if all(abs(r - D("0.5")) < D("0.000001") for r in ratios):
            return "A_offpeak_in_peak", f"ratio≈0.5 по {bad}"
        if all(abs(r - D(2)) < D("0.000001") for r in ratios):
            return "peak_in_offpeak", f"ratio≈2 по {bad}"
    return "unclassified", f"компоненты: {bad}, max dev {dev}"


def _log(msg: str) -> None:
    print(msg, flush=True)


def reconcile(logs: pd.DataFrame, org_map: dict[str, str],
              sched: dict[tuple[str, str], list[Window]]) -> pd.DataFrame:
    alt_cache = {k: list({w.rate for w in ws}) for k, ws in sched.items()}
    records: list[dict[str, Any]] = []
    total, step = len(logs), max(1, len(logs) // 20)
    for i, row in enumerate(logs.itertuples(index=False), 1):
        rate = resolve_rate(row.organization_id, row.model_group, row.start_time, org_map, sched)
        ref = ref_components(row, rate)
        correct = _total(ref, row)
        plist = org_map.get(row.organization_id, "")
        kind, evidence = classify(row, ref, rate, alt_cache.get((plist, row.model_group), []))
        records.append({
            "request_id": row.request_id, "start_time": row.start_time,
            "client": row.client, "model_group": row.model_group, "status": row.status,
            "export_total": row.total_cost, "correct_total": correct,
            "delta": row.total_cost - correct, "error_type": kind, "evidence": evidence,
            "window": rate.window if rate else "—", "tariff": rate.label if rate else "zero",
        })
        if i % step == 0 or i == total:
            print(f"\r      расчёт: {i}/{total} ({i * 100 // total}%)", end="", flush=True)
    print()
    return pd.DataFrame(records)


# ---------------------------------------------------------------- проверки
def run_checks(logs: pd.DataFrame, org_map: dict[str, str],
               sched: dict[tuple[str, str], list[Window]]) -> list[CheckResult]:
    checks: list[CheckResult] = []

    dup = logs[logs.duplicated("request_id", keep=False)]
    checks.append(CheckResult("Дубли request_id", dup.empty,
                              f"{len(dup)} строк-дублей", len(dup)))

    nulls = logs[["request_id", "organization_id", "model_group", "start_time"]].isna().sum().sum()
    checks.append(CheckResult("NULL в ключевых полях", nulls == 0, f"{nulls} пропусков", int(nulls)))

    neg = sum((logs[c] < 0).sum() for c in TOKEN_COLS if c in logs)
    checks.append(CheckResult("Отрицательные токены", neg == 0, f"{neg} ячеек", int(neg)))

    bad_cached = (logs.cached_input_tokens > logs.prompt_tokens).sum()
    text = (logs.completion_tokens - logs.reasoning_tokens - logs.audio_input_tokens
            - logs.image_tokens - logs.output_image_tokens)
    bad_text = (text < 0).sum()
    checks.append(CheckResult("Инварианты токенов", bad_cached + bad_text == 0,
                              f"cached>prompt: {bad_cached}, text<0: {bad_text}",
                              int(bad_cached + bad_text)))

    lo = pd.Timestamp(config.PERIOD_START_UTC, tz="UTC")
    hi = pd.Timestamp(config.PERIOD_END_UTC, tz="UTC")
    out = ((logs.start_time < lo) | (logs.start_time >= hi)).sum()
    checks.append(CheckResult("Границы периода 13–14.10 UTC", out == 0,
                              f"{out} строк вне периода", int(out)))

    unknown_org = set(logs.organization_id) - set(org_map)
    checks.append(CheckResult("Организации в маппинге", not unknown_org,
                              f"неизвестные org: {unknown_org or 'нет'}", len(unknown_org)))

    known_models = {m for (_, m) in sched}
    zero_models = sorted(set(logs.model_group) - known_models)
    checks.append(CheckResult("Модели вне прайса (zero-rule)", True,
                              f"списываются в 0: {zero_models or 'нет'}", len(zero_models)))

    comp_sum = logs[COST_COLS[0]].copy()
    for c in COST_COLS[1:]:
        comp_sum = comp_sum + logs[c]
    internal = (comp_sum - logs["total_cost"]).abs() > Q8 * 2
    checks.append(CheckResult("total = сумма компонент (выгрузка)", not internal.any(),
                              f"{internal.sum()} битых total", int(internal.sum())))

    failures = (logs.status == "failure").sum()
    checks.append(CheckResult("failure-строки учтены", True,
                              f"{failures} failure включены в расчёт", int(failures)))
    return checks


# ---------------------------------------------------------------- main
def run(source: str) -> ReconcileResult:
    t0 = time.perf_counter()
    _log(f"[1/6] Загрузка: {source}")
    xls = load_workbook(source)
    _log(f"      OK, {time.perf_counter() - t0:.1f}s")

    _log("[2/6] Парсинг логов…")
    logs = parse_logs(xls)
    _log(f"      {len(logs)} строк")

    _log("[3/6] Парсинг прайса…")
    org_map, prices = parse_prices(xls)
    sched = build_schedule(prices)
    _log(f"      {len(org_map)} орг, {len(prices)} строк прайса, {len(sched)} моделей")

    _log("[4/6] Проверки данных…")
    checks = run_checks(logs, org_map, sched)
    _log(f"      {len(checks)} проверок, FAIL: {sum(not c.passed for c in checks)}")

    _log("[5/6] Эталонный расчёт…")
    result_df = reconcile(logs, org_map, sched)

    # Конвенция material (одинаково в Python и SQL):
    # значимое = |Δ| > 1e-6 ИЛИ структурное повреждение — независимое от суммы
    STRUCTURAL_TYPES = ("broken_total", "total_zeroed", "not_in_pricelist")
    material = result_df[
        (result_df["delta"].abs() > config.MATERIAL_DELTA)
        | result_df["error_type"].isin(STRUCTURAL_TYPES)
    ].copy()
    material = material.sort_values("delta", key=lambda s: s.abs(), ascending=False)
    sum_correct = sum(result_df.correct_total.tolist(), D(0))
    sum_export = sum(result_df.export_total.tolist(), D(0))
    totals = {"rows": D(len(result_df)), "correct": sum_correct, "export": sum_export,
              "delta": sum_export - sum_correct, "discrepancies": D(len(material))}
    stats = (material.groupby("error_type")
             .agg(count=("request_id", "size"), impact=("delta", lambda s: sum(s.tolist(), D(0))))
             .reset_index().sort_values("impact", key=lambda s: s.abs(), ascending=False))
    examples = {
        k: pd.concat([g.head(2), g.sample(n=min(1, len(g)), random_state=42)])
           .drop_duplicates("request_id").to_dict("records")
        for k, g in material.groupby("error_type")
    }
    _log(f"[6/6] Готово за {time.perf_counter() - t0:.1f}s")
    return ReconcileResult(source, datetime.now(timezone.utc), result_df, material,
                           checks, totals, stats, examples)


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="Сверка биллинга DeepSeek")
    parser.add_argument("--file", help="локальный .xlsx")
    parser.add_argument("--url", help="ссылка на Google-таблицу", default=config.SHEET_URL)
    args = parser.parse_args()
    source = args.file or args.url

    try:
        result = run(source)
    except (RuntimeError, FileNotFoundError, ValueError) as exc:
        raise SystemExit(f"Ошибка: {exc}")

    _log("Запись отчётов…")
    config.REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    # реестр генераторов: новый формат = функция + запись в reporting.REPORTERS
    for name, writer in reporting.REPORTERS.items():
        path = config.REPORTS_DIR / name
        writer(result, path)
        print(f"Отчёт: {path}")

    t = result.totals
    print(f"\nСтрок: {t['rows']:.0f} | расхождений: {t['discrepancies']:.0f}")
    print(f"Эталон:   {t['correct']:.6f} USD")
    print(f"Выгрузка: {t['export']:.6f} USD")
    print(f"Разница:  {t['delta']:+.6f} USD")


if __name__ == "__main__":
    main()