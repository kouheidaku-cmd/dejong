"""設定ファイルとパスの読み込み。"""
from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

ROOT = Path(__file__).resolve().parent.parent


def load_yaml(path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def load_settings(root: Path = ROOT) -> dict:
    return load_yaml(root / "config" / "settings.yaml")


def load_watchlist(root: Path = ROOT) -> list[dict]:
    return load_yaml(root / "config" / "watchlist.yaml").get("tickers") or []


def load_universe(root: Path = ROOT) -> list[str]:
    return [str(t) for t in load_yaml(root / "config" / "universe.yaml").get("tickers") or []]


def lot_size(ticker: str, settings: dict) -> int:
    sizes = settings.get("lot_size", {})
    for suffix, size in sizes.items():
        if suffix != "default" and ticker.endswith(suffix):
            return int(size)
    return int(sizes.get("default", 1))


def today(settings: dict) -> date:
    return datetime.now(ZoneInfo(settings.get("timezone", "UTC"))).date()
