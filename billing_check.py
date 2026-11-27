"""Сверка биллинга DeepSeek: эталонный расчёт по прайсу и поиск расхождений.

Деньги — Decimal (точная арифметика, воспроизведение округления выгрузки
ROUND_HALF_UP до 8 знаков). pandas — только загрузка/группировки.
"""
from __future__ import annotations

import argparse
import io
import re
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
    """Токены: '1,020' / 1020.0 / '' -> int."""
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return 0
    if isinstance(v, str):
        v = v.replace(",", "").replace(" ", "").strip()
        if not v:
            return 0
    return int(float(v))


def _to_dec(v: Any) -> Decimal:
    """Деньги/ставки: запятая как десятичный разделитель тоже допускается."""
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return D(0)
    s = str(v).strip().replace(" ", "").replace(",", ".")
    return D(s) if s else D(0)


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

    df["start_time"] = pd.to_datetime(df["start_time"], utc=True)
    for c in TOKEN_COLS:
        if c in df.columns:
            df[c] = df[c].map(_to_int)
    for c in (*COST_COLS, "total_cost"):
        df[c] = df[c].map(_to_dec)
    for c in ("request_id", "organization_id", "model_group", "client", "status"):
        df[c] = df[c].astype(str).str.strip()
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
        model = str(row.iloc[idx["Модель"]]).strip()
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
    """Возвращает (тип ошибки, доказательство). Подтверждение — пересчётом альтернативной ставкой."""
    exp = {c: getattr(row, c) for c in COST_COLS}
    ref_total = _total(ref, row)

    # битый total выгрузки (не равен сумме своих компонент)
    if abs(sum(exp.values(), D(0)) - row.total_cost) > Q8 * 2:
        return "broken_total", f"total={row.total_cost} != sum={sum(exp.values(), D(0))}"

    if rate is None:
        if any(exp[c] > 0 for c in REF_COLS):
            return "not_in_pricelist", "модель вне прайса, но списание > 0"
        return "ok", ""

    if all(q8(ref[c]) == exp[c] for c in REF_COLS) and abs(ref_total - row.total_cost) <= Q8 * 2:
        return "ok", ""

    bad = [c for c in REF_COLS if q8(ref[c]) != exp[c]]

    # весь запрос по альтернативной ставке (подтверждение точным пересчётом)
    for alt in alt_rates:
        if alt == rate:
            continue
        alt_ref = ref_components(row, alt)
        if all(q8(alt_ref[c]) == exp[c] for c in REF_COLS):
            kind = "A_offpeak_in_peak" if alt.input < rate.input else "peak_in_offpeak"
            return kind, f"выгрузка точно воспроизводится ставкой {alt.label} ({alt.window})"

    # только reasoning ×2
    if bad == ["reasoning_cost"] and exp["reasoning_cost"] == q8(ref["reasoning_cost"] * 2):
        if len(alt_rates) > 1:
            return "B_reasoning_peak_in_offpeak", "reasoning ×2 = peak-ставка в off-peak окне"
        return "C_reasoning_doubled", "reasoning ×2 у модели с одной ценой (окон нет)"

    if abs(row.total_cost - ref_total) <= config.MATERIAL_DELTA:
        return "rounding_noise", "расхождение в пределах округления выгрузки"
    return "unclassified", f"расходятся компоненты: {bad}"


def reconcile(logs: pd.DataFrame, org_map: dict[str, str],
              sched: dict[tuple[str, str], list[Window]]) -> pd.DataFrame:
    alt_cache: dict[tuple[str, str], list[Rate]] = {
        k: list({w.rate for w in ws}) for k, ws in sched.items()
    }
    records: list[dict[str, Any]] = []
    for row in logs.itertuples(index=False):
        rate = resolve_rate(row.organization_id, row.model_group, row.start_time, org_map, sched)
        ref = ref_components(row, rate)
        correct = _total(ref, row)
        plist = org_map.get(row.organization_id, "")
        alts = alt_cache.get((plist, row.model_group), [])
        kind, evidence = classify(row, ref, rate, alts)
        records.append({
            "request_id": row.request_id, "start_time": row.start_time,
            "client": row.client, "model_group": row.model_group, "status": row.status,
            "export_total": row.total_cost, "correct_total": correct,
            "delta": row.total_cost - correct, "error_type": kind, "evidence": evidence,
            "window": rate.window if rate else "—", "tariff": rate.label if rate else "zero",
        })
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

    internal = logs.apply(
        lambda r: abs(sum((r[c] for c in COST_COLS), D(0)) - r.total_cost) > Q8 * 2, axis=1)
    checks.append(CheckResult("total = сумма компонент (выгрузка)", not internal.any(),
                              f"{internal.sum()} битых total", int(internal.sum())))

    failures = (logs.status == "failure").sum()
    checks.append(CheckResult("failure-строки учтены", True,
                              f"{failures} failure включены в расчёт", int(failures)))
    return checks


# ---------------------------------------------------------------- main
def run(source: str) -> ReconcileResult:
    xls = load_workbook(source)
    logs = parse_logs(xls)
    org_map, prices = parse_prices(xls)
    sched = build_schedule(prices)

    checks = run_checks(logs, org_map, sched)
    result_df = reconcile(logs, org_map, sched)

    material = result_df[~result_df.error_type.isin(["ok", "rounding_noise"])].copy()
    material = material.sort_values("delta", key=lambda s: s.abs(), ascending=False)

    sum_correct = sum(result_df.correct_total.tolist(), D(0))
    sum_export = sum(result_df.export_total.tolist(), D(0))
    totals = {
        "rows": D(len(result_df)),
        "correct": sum_correct,
        "export": sum_export,
        "delta": sum_export - sum_correct,
        "discrepancies": D(len(material)),
    }

    stats = (material.groupby("error_type")
             .agg(count=("request_id", "size"), impact=("delta", lambda s: sum(s.tolist(), D(0))))
             .reset_index().sort_values("impact", key=lambda s: s.abs(), ascending=False))

    examples: dict[str, list[dict[str, Any]]] = {}
    for kind, grp in material.groupby("error_type"):
        examples[kind] = grp.head(2).to_dict("records")

    return ReconcileResult(
        source=source, generated_at=datetime.now(timezone.utc),
        logs=result_df, discrepancies=material, checks=checks,
        totals=totals, error_stats=stats, examples=examples,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Сверка биллинга DeepSeek")
    parser.add_argument("--file", help="локальный .xlsx")
    parser.add_argument("--url", help="ссылка на Google-таблицу", default=config.SHEET_URL)
    args = parser.parse_args()
    source = args.file or args.url

    try:
        result = run(source)
    except (RuntimeError, FileNotFoundError, ValueError) as exc:
        raise SystemExit(f"Ошибка: {exc}")

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