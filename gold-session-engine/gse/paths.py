"""Filesystem locations used by the bot.

Everything the bot writes lives under one data directory (``GSE_DATA_DIR`` or
``./data``) so a VPS install is easy to back up and nothing sensitive ends up in git.
"""

from __future__ import annotations

import os
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent
PROJECT_DIR = PACKAGE_DIR.parent


def data_dir() -> Path:
    root = Path(os.environ.get("GSE_DATA_DIR", PROJECT_DIR / "data"))
    root.mkdir(parents=True, exist_ok=True)
    return root


def config_path() -> Path:
    return Path(os.environ.get("GSE_CONFIG", PROJECT_DIR / "config" / "settings.yaml"))


def secrets_path() -> Path:
    return data_dir() / "secrets.json"


def market_dir() -> Path:
    p = data_dir() / "market"
    p.mkdir(parents=True, exist_ok=True)
    return p


def news_dir() -> Path:
    p = data_dir() / "news"
    p.mkdir(parents=True, exist_ok=True)
    return p


def backtests_dir() -> Path:
    p = data_dir() / "backtests"
    p.mkdir(parents=True, exist_ok=True)
    return p


def journal_path() -> Path:
    return data_dir() / "journal.sqlite3"


def logs_dir() -> Path:
    p = data_dir() / "logs"
    p.mkdir(parents=True, exist_ok=True)
    return p
