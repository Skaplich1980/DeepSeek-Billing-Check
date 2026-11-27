"""Демо SQL-решения на SQLite (чтобы не разворачивать Postgres/MySQL).

Создаёт БД с теми же полями, грузит данные из того же Excel (переиспользует
загрузчики billing_check) и прогоняет SQL-сверку. Отличия от MySQL-версии
(sql_solution.md): WEEKDAY() -> strftime, DECIMAL -> целые единицы 1e-10 USD
(точная арифметика без float).
"""
from __future__ import annotations

import argparse
import sqlite3
from decimal import Decimal
from pathlib import Path

import config
from billing_check import load_workbook, parse_logs, parse_prices, build_schedule

DDL = """
DROP TABLE IF EXISTS raw_logs;
DROP TABLE IF EXISTS org_prices;
DROP TABLE IF EXISTS price_windows;

-- staging БЕЗ unique по request_id: дубли — улика, их нельзя терять при загрузке
CREATE TABLE raw_logs (
    start_time TEXT NOT NULL,              -- UTC, DATETIME-аналог
    request_id TEXT NOT NULL,
    organization_id TEXT NOT NULL,
    model_group TEXT NOT NULL,
    status TEXT NOT NULL,
    prompt_tokens INTEGER NOT NULL,
    completion_tokens INTEGER NOT NULL,
    cached_input_tokens INTEGER NOT NULL,
    reasoning_tokens INTEGER NOT NULL,
    audio_input_tokens INTEGER NOT NULL,
    image_tokens INTEGER NOT NULL,
    output_image_tokens INTEGER NOT NULL,
    accepted_prediction_tokens INTEGER NOT NULL,
    rejected_prediction_tokens INTEGER NOT NULL,
    input_cost REAL NOT NULL,              -- cost-колонки выгрузки как в источнике
    cached_input_cost REAL NOT NULL,
    output_cost REAL NOT NULL,
    reasoning_cost REAL NOT NULL,
    prediction_cost REAL NOT NULL,
    image_cost REAL NOT NULL,
    cache_creation_cost REAL NOT NULL,
    web_search_cost REAL NOT NULL,
    total_cost REAL NOT NULL
);
CREATE INDEX idx_request ON raw_logs(request_id);

CREATE TABLE org_prices (
    organization_id TEXT PRIMARY KEY,
    price_list TEXT NOT NULL
);

-- расписание развёрнуто: строка = (прайс, модель, день недели, окно)
-- ставки INTEGER = $/1M * 10^4 -> стоимость tokens*rate в единицах 1e-10 USD (точно)
CREATE TABLE price_windows (
    price_list TEXT NOT NULL,
    model_group TEXT NOT NULL,
    weekday INTEGER NOT NULL,              -- 0=Пн … 6=Вс
    h_from INTEGER NOT NULL,
    h_to INTEGER NOT NULL,                 -- конец исключён
    input_rate INTEGER NOT NULL,
    output_rate INTEGER NOT NULL,
    cache_rate INTEGER NOT NULL,
    PRIMARY KEY (price_list, model_group, weekday, h_from)
);
"""

#环境与 SQLite: Monday-based weekday и час из строки UTC
WD = "((CAST(strftime('%w', start_time) AS INTEGER) + 6) % 7)"
HR = "CAST(strftime('%H', start_time) AS INTEGER)"
SCALE = 10_000_000_000  # 1e10: перевод REAL-стоимости в целые единицы

RECONCILE_SQL = f"""
WITH rated AS (
    SELECT l.*,
           COALESCE(w.input_rate, 0)  AS ir,   -- LEFT JOIN + COALESCE = zero-rule
           COALESCE(w.output_rate, 0) AS orr,
           COALESCE(w.cache_rate, 0)  AS cr,
           (l.completion_tokens - l.reasoning_tokens - l.audio_input_tokens
            - l.image_tokens - l.output_image_tokens) AS text_tokens
    FROM raw_logs l
    JOIN org_prices o ON o.organization_id = l.organization_id
    LEFT JOIN price_windows w
           ON w.price_list = o.price_list
          AND w.model_group = l.model_group
          AND w.weekday = {WD}
          AND w.h_from <= {HR} AND w.h_to > {HR}
),
calc AS (
    SELECT request_id, start_time, model_group,
           CAST(ROUND(total_cost * {SCALE}) AS INTEGER) AS export_1e10,
           CAST(ROUND(input_cost * {SCALE}) AS INTEGER) AS exp_input,
           CAST(ROUND(reasoning_cost * {SCALE}) AS INTEGER) AS exp_reason,
           (prompt_tokens - cached_input_tokens) * ir AS ref_input,
           cached_input_tokens * cr                   AS ref_cached,
           text_tokens * orr                          AS ref_output,
           reasoning_tokens * orr                     AS ref_reason,
           (accepted_prediction_tokens + rejected_prediction_tokens) * orr AS ref_pred,
           image_tokens * ir                          AS ref_image,
           CAST(ROUND((cache_creation_cost + web_search_cost) * {SCALE}) AS INTEGER) AS extra,
           ir
    FROM rated
)
SELECT request_id, model_group,
       export_1e10 / 1e10 AS export_usd,
       (ref_input + ref_cached + ref_output + ref_reason + ref_pred + ref_image + extra) / 1e10 AS correct_usd,
       CASE
           WHEN ir IS NULL AND export_1e10 > extra THEN 'not_in_pricelist'
           WHEN ABS(2 * exp_input - ref_input) <= 100
            AND ABS(2 * (export_1e10 - extra) - (ref_input + ref_cached + ref_output + ref_reason + ref_pred + ref_image)) <= 400
               THEN 'A_offpeak_in_peak'
           WHEN ABS(exp_reason - 2 * ref_reason) <= 100 THEN 'reasoning_x2'
           ELSE 'unclassified'
       END AS error_type
FROM calc
WHERE ABS(export_1e10 - (ref_input + ref_cached + ref_output + ref_reason + ref_pred + ref_image + extra)) > 10000
ORDER BY ABS(export_1e10 - (ref_input + ref_cached + ref_output + ref_reason + ref_pred + ref_image + extra)) DESC;
"""

TOTALS_SQL = f"""
WITH rated AS (
    SELECT l.total_cost,
           (l.prompt_tokens - l.cached_input_tokens) * COALESCE(w.input_rate, 0)
           + l.cached_input_tokens * COALESCE(w.cache_rate, 0)
           + (l.completion_tokens - l.reasoning_tokens - l.audio_input_tokens
              - l.image_tokens - l.output_image_tokens) * COALESCE(w.output_rate, 0)
           + l.reasoning_tokens * COALESCE(w.output_rate, 0)
           + (l.accepted_prediction_tokens + l.rejected_prediction_tokens) * COALESCE(w.output_rate, 0)
           + l.image_tokens * COALESCE(w.input_rate, 0)
           + CAST(ROUND((l.cache_creation_cost + l.web_search_cost) * {SCALE}) AS INTEGER) AS ref_1e10
    FROM raw_logs l
    JOIN org_prices o ON o.organization_id = l.organization_id
    LEFT JOIN price_windows w
           ON w.price_list = o.price_list AND w.model_group = l.model_group
          AND w.weekday = {WD} AND w.h_from <= {HR} AND w.h_to > {HR}
)
SELECT SUM(ref_1e10) / 1e10 AS correct_usd,
       SUM(total_cost)      AS export_usd,
       SUM(total_cost) - SUM(ref_1e10) / 1e10 AS delta_usd
FROM rated;
"""

DUPLICATES_SQL = """
SELECT request_id, COUNT(*) AS n, SUM(total_cost) AS dup_sum
FROM raw_logs GROUP BY request_id HAVING COUNT(*) > 1;
"""

COVERAGE_SQL = """
SELECT price_list, model_group, SUM(h_to - h_from) AS hours
FROM price_windows GROUP BY price_list, model_group HAVING hours <> 168;
"""


def create_db(db_path: Path, source: str) -> None:
    xls = load_workbook(source)
    logs = parse_logs(xls)
    org_map, prices = parse_prices(xls)
    sched = build_schedule(prices)

    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db_path)
    try:
        con.executescript(DDL)
        con.executemany("INSERT INTO org_prices VALUES (?, ?)", org_map.items())

        win_rows: list[tuple] = []
        for (plist, model), windows in sched.items():
            for w in windows:
                for wd in sorted(w.weekdays):
                    win_rows.append((
                        plist, model, wd, w.h_from, w.h_to,
                        int(w.rate.input * Decimal(SCALE)),
                        int(w.rate.output * Decimal(SCALE)),
                        int(w.rate.cache_read * Decimal(SCALE)),
                    ))
        con.executemany("INSERT INTO price_windows VALUES (?,?,?,?,?,?,?,?)", win_rows)

        log_rows = [
            (r.start_time.strftime("%Y-%m-%d %H:%M:%S.%f"), r.request_id, r.organization_id,
             r.model_group, r.status, r.prompt_tokens, r.completion_tokens, r.cached_input_tokens,
             r.reasoning_tokens, r.audio_input_tokens, r.image_tokens, r.output_image_tokens,
             r.accepted_prediction_tokens, r.rejected_prediction_tokens,
             float(r.input_cost), float(r.cached_input_cost), float(r.output_cost),
             float(r.reasoning_cost), float(r.prediction_cost), float(r.image_cost),
             float(r.cache_creation_cost), float(r.web_search_cost), float(r.total_cost))
            for r in logs.itertuples(index=False)
        ]
        con.executemany("INSERT INTO raw_logs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        log_rows)
        con.commit()
        print(f"БД создана: {db_path} ({len(log_rows)} строк, {len(win_rows)} окон)")

        print("\n--- Итоги (SQL) ---")
        for row in con.execute(TOTALS_SQL):
            print(f"эталон={row[0]:.6f}  выгрузка={row[1]:.6f}  Δ={row[2]:+.6f}")

        print("\n--- Расхождения (SQL) ---")
        rows = list(con.execute(RECONCILE_SQL))
        for r in rows[:15]:
            print(f"{r[0]:45s} {r[1]:28s} export={r[2]:.8f} correct={r[3]:.8f} {r[4]}")
        print(f"всего расхождений: {len(rows)}")

        print("\n--- Дубли ---")
        dups = list(con.execute(DUPLICATES_SQL))
        print(dups or "нет")

        print("\n--- Покрытие расписания (должно быть пусто) ---")
        print(list(con.execute(COVERAGE_SQL)) or "OK: все модели покрыты на 168 ч")
    finally:
        con.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="SQLite-демо SQL-сверки")
    parser.add_argument("--file", help="локальный .xlsx")
    parser.add_argument("--url", default=config.SHEET_URL)
    parser.add_argument("--db", type=Path, default=config.SQL_DEMO_DB)
    args = parser.parse_args()
    create_db(args.db, args.file or args.url)


if __name__ == "__main__":
    main()