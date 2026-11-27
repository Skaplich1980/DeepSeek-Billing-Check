"""Точка входа для сверки биллинга DeepSeek.

Запуск:
    python main.py                     # скачает Google-таблицу из config.SHEET_URL
    python main.py --file path.xlsx    # локальный файл
"""
import sys

from billing_check import main

if __name__ == "__main__":
    sys.exit(main())
