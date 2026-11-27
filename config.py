"""Настройки сверки: все ссылки, пороги и пути в одном месте."""
from decimal import Decimal
from pathlib import Path

# Ссылка на задание (подставляется в шапку отчёта)
TASK_DOC_URL: str = "https://docs.google.com/document/d/1orjAtz-K78S4FfqPx9NzJeWSJ3gFpAQ5/edit"
# Данные: публичная Google-таблица (скрипт сам скачает export?format=xlsx)
SHEET_URL: str = "https://docs.google.com/spreadsheets/d/1_PDm-vGvn_uZiYDBQh_t3CNFeqx1wpNo/"

SHEET_LOGS: str = "Сырые логи"
SHEET_PRICES: str = "Цены по организациям"

PERIOD_START_UTC: str = "2026-10-13 00:00:00"
PERIOD_END_UTC: str = "2026-10-15 00:00:00"

# Порог значимости: выгрузка округляет компоненты до 8 знаков (шум <= ~4e-8),
# поэтому расхождением считаем |delta| > 1e-6
MATERIAL_DELTA: Decimal = Decimal("0.000001")
ROUND_SCALE: Decimal = Decimal("0.00000001")  # точность выгрузки — 8 знаков

REPORTS_DIR: Path = Path("reports")
SQL_DEMO_DB: Path = REPORTS_DIR / "billing_demo.db"