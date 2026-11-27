"""SQLite-демо SQL-решения: те же запросы, что в sql_solution.md (PostgreSQL).

Деньги в целых единицах 1e-10 USD, wd/hh вычислены при загрузке -> запросы портируемые.
Переиспользует загрузчики billing_check (одни данные, два движка — кросс-проверка).
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

import config
from billing_check import load_workbook, parse_logs, parse_prices, build_schedule

SCALE = Decimal(10**10)


def _scaled(x: Decimal) -> int:
    return int((x * SCALE).to_integral_value(rounding=ROUND_HALF_UP))

DDL = """
DROP TABLE IF EXISTS raw_logs; DROP TABLE IF EXISTS org_prices; DROP TABLE IF EXISTS price_windows;
CREATE TABLE raw_logs (
    start_time TEXT NOT NULL, wd INTEGER NOT NULL, hh INTEGER NOT NULL,
    request_id TEXT NOT NULL, organization_id TEXT NOT NULL, model_group TEXT NOT NULL,
    status TEXT NOT NULL,
    prompt_tokens INTEGER NOT NULL, completion_tokens INTEGER NOT NULL,
    cached_input_tokens INTEGER NOT NULL, reasoning_tokens INTEGER NOT NULL,
    audio_input_tokens INTEGER NOT NULL, image_tokens INTEGER NOT NULL,
    output_image_tokens INTEGER NOT NULL,
    accepted_prediction_tokens INTEGER NOT NULL, rejected_prediction_tokens INTEGER NOT NULL,
    input_cost INTEGER NOT NULL, cached_input_cost INTEGER NOT NULL,
    output_cost INTEGER NOT NULL, reasoning_cost INTEGER NOT NULL,
    prediction_cost INTEGER NOT NULL, image_cost INTEGER NOT NULL,
    cache_creation_cost INTEGER NOT NULL, web_search_cost INTEGER NOT NULL,
    total_cost INTEGER NOT NULL
);
CREATE INDEX idx_logs_request ON raw_logs(request_id);
CREATE TABLE org_prices (organization_id TEXT PRIMARY KEY, price_list TEXT NOT NULL);
CREATE TABLE price_windows (
    price_list TEXT NOT NULL, model_group TEXT NOT NULL,
    weekday INTEGER NOT NULL, h_from INTEGER NOT NULL, h_to INTEGER NOT NULL,
    input_rate INTEGER NOT NULL, output_rate INTEGER NOT NULL, cache_rate INTEGER NOT NULL,
    PRIMARY KEY (price_list, model_group, weekday, h_from)
);
"""

RECONCILE_SQL = """
WITH rated AS (
    SELECT l.*, COALESCE(w.input_rate,0) AS ir, COALESCE(w.output_rate,0) AS orr,
           COALESCE(w.cache_rate,0) AS cr,
           CASE WHEN w.input_rate IS NULL THEN 1 ELSE 0 END AS no_rate,
           (l.completion_tokens-l.reasoning_tokens-l.audio_input_tokens-l.image_tokens-l.output_image_tokens) AS text_tokens
    FROM raw_logs l
    JOIN org_prices o ON o.organization_id=l.organization_id
    LEFT JOIN price_windows w
           ON w.price_list=o.price_list AND w.model_group=l.model_group
          AND w.weekday=l.wd AND w.h_from<=l.hh AND w.h_to>l.hh
),
calc AS (
    SELECT request_id, start_time, model_group, total_cost AS export_1e10,
           input_cost, reasoning_cost, no_rate,
           cached_input_cost, output_cost, prediction_cost, image_cost,
           input_cost+cached_input_cost+output_cost+reasoning_cost+prediction_cost+image_cost
           +cache_creation_cost+web_search_cost AS export_comp_sum,
           (prompt_tokens-cached_input_tokens)*ir AS input_ref,
           cached_input_tokens*cr AS cached_ref,
           text_tokens*orr AS output_ref,
           reasoning_tokens*orr AS reasoning_ref,
           (accepted_prediction_tokens+rejected_prediction_tokens)*orr AS prediction_ref,
           image_tokens*ir AS image_ref,
           cache_creation_cost+web_search_cost AS extra_1e10
    FROM rated
),
tot AS (
    SELECT *, (input_ref+cached_ref+output_ref+reasoning_ref+prediction_ref+image_ref+extra_1e10) AS correct_1e10
    FROM calc
)
SELECT request_id, model_group,
       export_1e10/10000000000.0, correct_1e10/10000000000.0,
       (export_1e10-correct_1e10)/10000000000.0,
       CASE
           WHEN no_rate=1 AND export_1e10>extra_1e10 THEN 'not_in_pricelist'
           WHEN ABS(export_1e10-export_comp_sum)>200 THEN
               CASE WHEN export_1e10=0
                     AND ABS(input_cost-input_ref)<=50
                     AND ABS(cached_input_cost-cached_ref)<=50
                     AND ABS(output_cost-output_ref)<=50
                     AND ABS(reasoning_cost-reasoning_ref)<=50
                     AND ABS(prediction_cost-prediction_ref)<=50
                     AND ABS(image_cost-image_ref)<=50
                    THEN 'total_zeroed' ELSE 'broken_total' END
           WHEN ABS(2*input_cost-input_ref)<=100
            AND ABS(2*(export_1e10-extra_1e10)-(correct_1e10-extra_1e10))<=400 THEN 'A_offpeak_in_peak'
           WHEN ABS(input_cost-2*input_ref)<=100
            AND ABS((export_1e10-extra_1e10)-2*(correct_1e10-extra_1e10))<=400 THEN 'peak_in_offpeak'
           WHEN reasoning_ref>0 AND ABS(reasoning_cost-2*reasoning_ref)<=100 THEN 'reasoning_x2'
           ELSE 'unclassified'
       END
FROM tot
WHERE ABS(export_1e10-correct_1e10)>10000
   OR ABS(export_1e10-export_comp_sum)>200
ORDER BY ABS(export_1e10-correct_1e10) DESC;
"""

TOTALS_SQL = """
WITH rated AS (
    SELECT l.total_cost,
           (l.prompt_tokens-l.cached_input_tokens)*COALESCE(w.input_rate,0)
         + l.cached_input_tokens*COALESCE(w.cache_rate,0)
         + (l.completion_tokens-l.reasoning_tokens-l.audio_input_tokens-l.image_tokens-l.output_image_tokens)*COALESCE(w.output_rate,0)
         + l.reasoning_tokens*COALESCE(w.output_rate,0)
         + (l.accepted_prediction_tokens+l.rejected_prediction_tokens)*COALESCE(w.output_rate,0)
         + l.image_tokens*COALESCE(w.input_rate,0)
         + l.cache_creation_cost + l.web_search_cost AS ref_1e10
    FROM raw_logs l
    JOIN org_prices o ON o.organization_id=l.organization_id
    LEFT JOIN price_windows w
           ON w.price_list=o.price_list AND w.model_group=l.model_group
          AND w.weekday=l.wd AND w.h_from<=l.hh AND w.h_to>l.hh
)
SELECT SUM(ref_1e10)/10000000000.0, SUM(total_cost)/10000000000.0,
       (SUM(total_cost)-SUM(ref_1e10))/10000000000.0 FROM rated;
"""

DUPLICATES_SQL = """
SELECT request_id, COUNT(*), SUM(total_cost)/10000000000.0
FROM raw_logs GROUP BY request_id HAVING COUNT(*)>1;
"""
COVERAGE_SQL = """
SELECT price_list, model_group, SUM(h_to-h_from) FROM price_windows
GROUP BY price_list, model_group HAVING SUM(h_to-h_from)<>168;
"""
INVARIANTS_SQL = """
SELECT request_id FROM raw_logs
WHERE cached_input_tokens>prompt_tokens
   OR (completion_tokens-reasoning_tokens-audio_input_tokens-image_tokens-output_image_tokens)<0;
"""


def create_db(db_path: Path, source: str) -> None:
    t0 = time.perf_counter()
    print(f"[1/4] Загрузка и парсинг: {source}", flush=True)
    xls = load_workbook(source)
    logs = parse_logs(xls)
    org_map, prices = parse_prices(xls)
    sched = build_schedule(prices)
    print(f"      {len(logs)} строк, {len(sched)} моделей", flush=True)

    print("[2/4] Создание SQLite и загрузка…", flush=True)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db_path)
    try:
        con.executescript(DDL)
        con.executemany("INSERT INTO org_prices VALUES (?,?)", org_map.items())
        win_rows = [
            (pl, m, wd, w.h_from, w.h_to,
             _scaled(w.rate.input), _scaled(w.rate.output), _scaled(w.rate.cache_read))
            for (pl, m), ws in sched.items() for w in ws for wd in sorted(w.weekdays)
        ]
        con.executemany("INSERT INTO price_windows VALUES (?,?,?,?,?,?,?,?)", win_rows)
        log_rows = [
            (r.start_time.strftime("%Y-%m-%d %H:%M:%S.%f"), r.start_time.weekday(), r.start_time.hour,
             r.request_id, r.organization_id, r.model_group, r.status,
             r.prompt_tokens, r.completion_tokens, r.cached_input_tokens, r.reasoning_tokens,
             r.audio_input_tokens, r.image_tokens, r.output_image_tokens,
             r.accepted_prediction_tokens, r.rejected_prediction_tokens,
             _scaled(r.input_cost), _scaled(r.cached_input_cost), _scaled(r.output_cost),
             _scaled(r.reasoning_cost), _scaled(r.prediction_cost), _scaled(r.image_cost),
             _scaled(r.cache_creation_cost), _scaled(r.web_search_cost), _scaled(r.total_cost))
            for r in logs.itertuples(index=False)
        ]
        con.executemany("INSERT INTO raw_logs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        log_rows)
        con.commit()
        print(f"      БД: {db_path} ({len(log_rows)} строк, {len(win_rows)} окон)", flush=True)

        print("[3/4] SQL-сверка…", flush=True)
        row = con.execute(TOTALS_SQL).fetchone()
        print(f"\n--- Итоги (SQL) ---\nэталон={row[0]:.6f}  выгрузка={row[1]:.6f}  Δ={row[2]:+.6f}")
        rows = con.execute(RECONCILE_SQL).fetchall()
        print(f"\n--- Расхождения (SQL): {len(rows)} ---")
        for r in rows[:15]:
            print(f"{r[0]:45s} {r[1]:28s} export={r[2]:.8f} correct={r[3]:.8f} Δ={r[4]:+.8f} {r[5]}")
        print(f"\n--- Дубли ---\n{con.execute(DUPLICATES_SQL).fetchall() or 'нет'}")
        print(f"--- Покрытие 168ч ---\n{con.execute(COVERAGE_SQL).fetchall() or 'OK'}")
        print(f"--- Инварианты токенов ---\n{con.execute(INVARIANTS_SQL).fetchall() or 'OK'}")
        print(f"\n[4/4] Готово за {time.perf_counter() - t0:.1f}s. "
              f"Сравни итоги с billing_check.py — должны совпасть.", flush=True)
    finally:
        con.close()


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    p = argparse.ArgumentParser(description="SQLite-демо SQL-сверки (запросы как в PostgreSQL)")
    p.add_argument("--file"); p.add_argument("--url", default=config.SHEET_URL)
    p.add_argument("--db", type=Path, default=config.SQL_DEMO_DB)
    a = p.parse_args()
    create_db(a.db, a.file or a.url)


if __name__ == "__main__":
    main()