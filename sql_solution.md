# SQL-решение сверки биллинга DeepSeek (PostgreSQL)

Альтернатива Python-решению ([README](README.md)). Логика entirely в SQL.
**Запросы портируемые:** один и тот же текст работает в PostgreSQL 15+ и в SQLite 3
(демо `python sql_demo.py`, без разворачивания Postgres).

## Почему запросы одинаковые на обоих движках
- День недели (`wd`, 0=Пн…6=Вс) и час (`hh`) вычислены при загрузке → в запросе нет
  `EXTRACT`/`strftime`, только equi+range join.
- Деньги — **целые единицы 1e-10 USD** (BIGINT / INTEGER): ставки = $/1M × 10⁴,
  стоимость = tokens × rate. Точно на обоих движках, без NUMERIC/REAL-расхождений.
  В боевом Postgres можно хранить NUMERIC(20,12) — логика запроса та же.

## Нюансы (учтены)
- text_tokens: взята формула **задания** (`− audio_input_tokens − image_tokens − output_image_tokens`);
  лист пишет `− audio − image − video`, колонки video нет.
- Ставки из строк таблицы, не из прозы «окна = off-peak» (0731 t00-01 = peak, ali выходные = peak).
- Модель вне прайса → ноль (`LEFT JOIN + COALESCE`). cache_creation/web_search — из выгрузки.

## DDL (PostgreSQL)
```sql
CREATE TABLE raw_logs (
    start_time TIMESTAMPTZ NOT NULL,
    wd SMALLINT NOT NULL, hh SMALLINT NOT NULL,
    request_id TEXT NOT NULL,
    organization_id TEXT NOT NULL,
    model_group TEXT NOT NULL,
    status TEXT NOT NULL,
    prompt_tokens BIGINT NOT NULL, completion_tokens BIGINT NOT NULL,
    cached_input_tokens BIGINT NOT NULL, reasoning_tokens BIGINT NOT NULL,
    audio_input_tokens BIGINT NOT NULL, image_tokens BIGINT NOT NULL,
    output_image_tokens BIGINT NOT NULL,
    accepted_prediction_tokens BIGINT NOT NULL, rejected_prediction_tokens BIGINT NOT NULL,
    -- cost-колонки в целых единицах 1e-10 USD
    input_cost BIGINT NOT NULL, cached_input_cost BIGINT NOT NULL,
    output_cost BIGINT NOT NULL, reasoning_cost BIGINT NOT NULL,
    prediction_cost BIGINT NOT NULL, image_cost BIGINT NOT NULL,
    cache_creation_cost BIGINT NOT NULL, web_search_cost BIGINT NOT NULL,
    total_cost BIGINT NOT NULL
);
CREATE INDEX idx_logs_request ON raw_logs(request_id);
CREATE INDEX idx_logs_org_model_time ON raw_logs(organization_id, model_group, start_time);

CREATE TABLE org_prices (
    organization_id TEXT PRIMARY KEY, price_list TEXT NOT NULL
);
CREATE TABLE price_windows (
    price_list TEXT NOT NULL, model_group TEXT NOT NULL,
    weekday SMALLINT NOT NULL, h_from SMALLINT NOT NULL, h_to SMALLINT NOT NULL,
    input_rate BIGINT NOT NULL, output_rate BIGINT NOT NULL, cache_rate BIGINT NOT NULL,
    PRIMARY KEY (price_list, model_group, weekday, h_from)
);
```
SQLite: `TIMESTAMPTZ→TEXT`, `SMALLINT/BIGINT→INTEGER`, остальные идентичны.

## Основной запрос (PostgreSQL = SQLite)
```sql
WITH rated AS (
    SELECT l.*,
           COALESCE(w.input_rate,0) AS ir, COALESCE(w.output_rate,0) AS orr,
           COALESCE(w.cache_rate,0) AS cr,
           CASE WHEN w.input_rate IS NULL THEN 1 ELSE 0 END AS no_rate,
           (l.completion_tokens - l.reasoning_tokens - l.audio_input_tokens
            - l.image_tokens - l.output_image_tokens) AS text_tokens
    FROM raw_logs l
    JOIN org_prices o ON o.organization_id = l.organization_id
    LEFT JOIN price_windows w
           ON w.price_list = o.price_list AND w.model_group = l.model_group
          AND w.weekday = l.wd AND w.h_from <= l.hh AND w.h_to > l.hh
),
calc AS (
    SELECT request_id, start_time, model_group, total_cost AS export_1e10,
           input_cost, reasoning_cost, no_rate,
           cached_input_cost, output_cost, prediction_cost, image_cost,
           input_cost+cached_input_cost+output_cost+reasoning_cost+prediction_cost
           +image_cost+cache_creation_cost+web_search_cost AS export_comp_sum,
           (prompt_tokens - cached_input_tokens)*ir AS input_ref,
           cached_input_tokens*cr AS cached_ref,
           text_tokens*orr AS output_ref,
           reasoning_tokens*orr AS reasoning_ref,
           (accepted_prediction_tokens + rejected_prediction_tokens)*orr AS prediction_ref,
           image_tokens*ir AS image_ref,
           cache_creation_cost + web_search_cost AS extra_1e10
    FROM rated
),
tot AS (
    SELECT *, (input_ref+cached_ref+output_ref+reasoning_ref+prediction_ref+image_ref+extra_1e10) AS correct_1e10
    FROM calc
)
SELECT request_id, model_group,
       export_1e10  / 10000000000.0 AS export_usd,
       correct_1e10 / 10000000000.0 AS correct_usd,
       (export_1e10 - correct_1e10) / 10000000000.0 AS delta_usd,
       CASE
           WHEN no_rate = 1 AND export_1e10 > extra_1e10 THEN 'not_in_pricelist'
           WHEN ABS(export_1e10 - export_comp_sum) > 200 THEN
               CASE WHEN export_1e10 = 0
                     AND ABS(input_cost - input_ref) <= 50
                     AND ABS(cached_input_cost - cached_ref) <= 50
                     AND ABS(output_cost - output_ref) <= 50
                     AND ABS(reasoning_cost - reasoning_ref) <= 50
                     AND ABS(prediction_cost - prediction_ref) <= 50
                     AND ABS(image_cost - image_ref) <= 50
                    THEN 'total_zeroed' ELSE 'broken_total' END
           WHEN ABS(2*input_cost - input_ref) <= 100
            AND ABS(2*(export_1e10-extra_1e10) - (correct_1e10-extra_1e10)) <= 400
               THEN 'A_offpeak_in_peak'
           WHEN ABS(input_cost - 2*input_ref) <= 100
            AND ABS((export_1e10-extra_1e10) - 2*(correct_1e10-extra_1e10)) <= 400
               THEN 'peak_in_offpeak'
           WHEN reasoning_ref > 0 AND ABS(reasoning_cost - 2*reasoning_ref) <= 100
               THEN 'reasoning_x2'
           ELSE 'unclassified'
       END AS error_type
FROM tot
WHERE ABS(export_1e10 - correct_1e10) > 10000        -- порог 1e-6 USD
   OR ABS(export_1e10 - export_comp_sum) > 200       -- структурные: значимы независимо от суммы
ORDER BY ABS(export_1e10 - correct_1e10) DESC;
```

Конвенция material одинакова в обоих движках: значимое расхождение — `|Δ| > 1e-6 USD`
**ИЛИ** структурное повреждение (`broken_total` / `total_zeroed` / `not_in_pricelist`).
Гард «компоненты корректны» (≤50 = 5e-9) в ветке `total_zeroed` обязателен: строки с
total=0 и битыми компонентами получают `broken_total`, а не `total_zeroed`.

## Итоги
```sql
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
SELECT SUM(ref_1e10)/10000000000.0 AS correct_usd,
       SUM(total_cost)/10000000000.0 AS export_usd,
       (SUM(total_cost)-SUM(ref_1e10))/10000000000.0 AS delta_usd
FROM rated;
```

## Проверки
```sql
-- дубли (post-failure replay)
SELECT request_id, COUNT(*) n, SUM(total_cost)/10000000000.0 dup_sum
FROM raw_logs GROUP BY request_id HAVING COUNT(*)>1;

-- внутренняя согласованность выгрузки
SELECT request_id FROM raw_logs
WHERE ABS(total_cost-(input_cost+cached_input_cost+output_cost+reasoning_cost
      +prediction_cost+image_cost+cache_creation_cost+web_search_cost)) > 200;

-- самопроверка расписания: покрытие 168 ч
SELECT price_list, model_group, SUM(h_to-h_from) hours
FROM price_windows GROUP BY price_list, model_group HAVING hours<>168;

-- инварианты токенов
SELECT request_id FROM raw_logs
WHERE cached_input_tokens>prompt_tokens
   OR (completion_tokens-reasoning_tokens-audio_input_tokens-image_tokens-output_image_tokens)<0;
```

## План (PostgreSQL)
`EXPLAIN (ANALYZE, BUFFERS)`: Seq Scan raw_logs (N) → Hash Join org_prices (PK) →
Nested Loop / Hash Join price_windows (PK price_list+model_group+weekday, фильтр по hh).
Один set-based проход O(N). Полный скан оптимален для разовой сверки; индекс
`(organization_id, model_group, start_time)` — для инкрементальной по периоду.