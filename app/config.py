import os
from pathlib import Path

APP_VERSION = os.getenv("APP_VERSION", "PM-2026.11")
DATABASE_PATH = Path(os.getenv("DATABASE_PATH", "data/runs.sqlite3"))
RULE_VERSION = "procurematch-rules-1"
TIMEZONE = "Asia/Shanghai"

TAX_CONVENTIONS = ("line_by_line", "whole_invoice")
DEFAULT_TAX_CONVENTION = "line_by_line"

