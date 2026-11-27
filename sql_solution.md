# SQL-решение сверки биллинга DeepSeek

Альтернатива основному Python-решению ([README](README.md)). Вся логика — в SQL;
загрузчик только перекладывает Excel в staging-таблицы. В продакшене логи уже в БД —
SQL является естественным движком регулярной сверки.

**Демо без разворачивания Postgres/MySQL:** `python sql_demo.py` создаёт SQLite-БД
с теми же полями, грузит данные из того же Excel и прогоняет запросы ниже.

## Нюансы, учтённые в решении
- **Формула text_tokens:** лист пишет `completion − reasoning − audio − image − video`,
  задание — `− audio_input_tokens − image_tokens − output_image_tokens`. Колонки `video`
  нет, `audio_output_tokens` задание не вычитает. Использована **формула задания**.
- **Проза vs таблица:** «базовая цена = peak, окна = off-peak» не выполняется буквально
  (0731 t00-01 = peak; ali выходные = peak). Ставки берутся из строк таблицы, не из прозы.
- Модели вне прайса организации → списание в ноль (`LEFT JOIN + COALESCE`).
- `cache_creation_cost`, `web_search_cost` — из выгрузки.

## MySQL 8.0 — каноническая версия

### Staging
```sql
-- без unique по request_id: дубли — улика, PK отклонил бы их при загрузке
CREATE TABLE raw_logs (
    start_time DATETIME(6) NOT NULL,        -- именно DATETIME: TIMESTAMP конвертируется через session tz
    request_id VARCHAR(64) NOT NULL,
    organization_id CHAR(36) NOT NULL,
    model_group VARCHAR(64) NOT NULL,
    status VARCHAR(16) NOT NULL,
    prompt_tokens BIGINT NOT NULL,
    completion_tokens BIGINT NOT NULL,
    cached_input_tokens BIGINT NOT NULL,
    reasoning_tokens BIGINT NOT NULL,
    audio_input_tokens BIGINT NOT NULL,
    image_tokens BIGINT NOT NULL,
    output_image_tokens BIGINT NOT NULL,
    accepted_prediction_tokens BIGINT NOT NULL,
    rejected_prediction_tokens BIGINT NOT NULL,
    input_cost DECIMAL(18,8) NOT NULL,
    cached_input_cost DECIMAL(18,8) NOT NULL,
    output_cost DECIMAL(18,8) NOT NULL,
    reasoning_cost DECIMAL(18,8) NOT NULL,
    prediction_cost DECIMAL(18,8) NOT NULL,
    image_cost DECIMAL(18,8) NOT NULL,
    cache_creation_cost DECIMAL(18,8) NOT NULL,
    web_search_cost DECIMAL(18,8) NOT NULL,
    total_cost DECIMAL(18,8) NOT NULL,
    KEY idx_request (request_id),
    KEY idx_org_model_time (organization_id, model_group, start_time)
);
CREATE TABLE org_prices (
    organization_id CHAR(36) PRIMARY KEY,
    price_list VARCHAR(32) NOT NULL
);
-- расписание развёрнуто в строки: t06-10 mon-fri -> 5 строк; w00-24 sat,sun -> 2; "одна цена" -> 7
CREATE TABLE price_windows (
    price_list VARCHAR(32) NOT NULL,
    model_group VARCHAR(64) NOT NULL,
    weekday TINYINT NOT NULL,               -- 0=Пн … 6=Вс = WEEKDAY()
    h_from TINYINT NOT NULL,
    h_to TINYINT NOT NULL,                  -- конец исключён
    input_rate DECIMAL(20,12) NOT NULL,     -- ставка ЗА ТОКЕН = $/1M / 1e6 (считать при загрузке!)
    output_rate DECIMAL(20,12) NOT NULL,
    cache_rate DECIMAL(20,12) NOT NULL,
    PRIMARY KEY (price_list, model_group, weekday, h_from)
);
```

**Точность деления:** `0.0039/1000000` в DECIMAL(10,4) даст scale 8 → обрезку в ноль.
Per-token ставки считать при загрузке в DECIMAL(20,12) либо `SET SESSION div_precision_increment=12`.
В основном запросе деления нет — только умножение токенов на ставку (DECIMAL точен).
`ROUND()` для DECIMAL в MySQL — half away from zero = ROUND_HALF_UP выгрузки.

### Основной запрос
```sql
WITH rated AS (
    SELECT l.*,
           COALESCE(w.input_rate,0) AS ir, COALESCE(w.output_rate,0) AS orr,
           COALESCE(w.cache_rate,0) AS cr,
           (l.completion_tokens - l.reasoning_tokens - l.audio_input_tokens
            - l.image_tokens - l.output_image_tokens) AS text_tokens
    FROM raw_logs l
    JOIN org_prices o ON o.organization_id = l.organization_id
    LEFT JOIN price_windows w
           ON w.price_list = o.price_list AND w.model_group = l.model_group
          AND w.weekday = WEEKDAY(l.start_time)
          AND w.h_from <= HOUR(l.start_time) AND w.h_to > HOUR(l.start_time)
),
calc AS (
    SELECT request_id, start_time, model_group, total_cost AS export_total,
           (prompt_tokens - cached_input_tokens)*ir AS input_ref,
           cached_input_tokens*cr AS cached_ref,
           text_tokens*orr AS output_ref,
           reasoning_tokens*orr AS reasoning_ref,
           (accepted_prediction_tokens + rejected_prediction_tokens)*orr AS prediction_ref,
           image_tokens*ir AS image_ref,
           cache_creation_cost, web_search_cost,
           (prompt_tokens - cached_input_tokens)*ir + cached_input_tokens*cr
           + text_tokens*orr + reasoning_tokens*orr
           + (accepted_prediction_tokens + rejected_prediction_tokens)*orr
           + image_tokens*ir + cache_creation_cost + web_search_cost AS correct_total
    FROM rated
)
SELECT request_id, start_time, model_group,
       export_total, ROUND(correct_total,8) AS correct_total,
       ROUND(export_total - correct_total,8) AS delta_usd,
       CASE
           WHEN ABS(input_cost - ROUND(input_ref*0.5,8)) < 0.00000002
            AND ABS(output_cost - ROUND(output_ref*0.5,8)) < 0.00000002 THEN 'A_offpeak_in_peak'
           WHEN ABS(reasoning_cost - ROUND(reasoning_ref*2,8)) < 0.00000002 THEN 'reasoning_x2'
           ELSE 'unclassified'
       END AS error_type
FROM calc
WHERE ABS(export_total - correct_total) > 0.000001;
```

### Проверки
```sql
-- дубли (post-failure replay)
SELECT request_id, COUNT(*) n, SUM(total_cost) dup_sum
FROM raw_logs GROUP BY request_id HAVING COUNT(*) > 1;

-- внутренняя согласованность выгрузки
SELECT request_id FROM raw_logs
WHERE ABS(total_cost - (input_cost+cached_input_cost+output_cost+reasoning_cost
      +prediction_cost+image_cost+cache_creation_cost+web_search_cost)) > 0.00000002;

-- самопроверка расписания: покрытие 168 ч
SELECT price_list, model_group, SUM(h_to-h_from) hours
FROM price_windows GROUP BY price_list, model_group HAVING hours <> 168;
```

### План и индексы
EXPLAIN основного запроса: `l: ALL (N)` → `o: eq_ref (1, PK)` → `w: ref (1–2, PK price_list+model_group+weekday, фильтр по часу)`.
Один set-based проход **O(N)**, без коррелированных подзапросов. Полный скан raw_logs оптимален
для разовой сверки; индекс `(organization_id, model_group, start_time)` — для инкрементальной по периоду.
Развёртка окон в строки делает join equi+range по индексу вместо парсинга `t06-10` в каждом ряду.

## SQLite-демо (`sql_demo.py`)
Отличия от MySQL: `WEEKDAY()` → `((strftime('%w')+6)%7)`, `HOUR()` → `strftime('%H')`;
вместо DECIMAL — **целые единицы 1e-10 USD** (ставки INTEGER = $/1M×10⁴, стоимость = tokens×rate),
арифметика точная без float. Загрузчик переиспользует `billing_check.parse_*`.