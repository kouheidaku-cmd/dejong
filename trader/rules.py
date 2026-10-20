"""config/rules.yaml の売買ルールの判定。バックテストと日次シグナルの両方がここを使う。"""
from __future__ import annotations

import math
from pathlib import Path

import pandas as pd

from .config import ROOT, load_yaml
from .indicators import rsi


def load_rules(path: Path | None = None) -> dict:
    return load_yaml(path or ROOT / "config" / "rules.yaml")


def features(df: pd.DataFrame) -> pd.DataFrame:
    """ルール判定に使う日次の特徴量。各行はその日の終値時点で分かる情報だけ。"""
    c = df["Close"]
    return pd.DataFrame({
        "open": df["Open"],
        "close": c,
        "sma20": c.rolling(20).mean(),
        "sma50": c.rolling(50).mean(),
        "sma200": c.rolling(200).mean(),
        "rsi14": rsi(c),
        "ret_60d": c / c.shift(60) - 1,
    }, index=df.index)


def _missing(row, *keys) -> bool:
    return any(row.get(k) is None or math.isnan(row[k]) for k in keys)


def entry_check(row: dict, rules: dict) -> list[str]:
    """新規買いの条件のうち満たしていないものを返す。空なら買い条件成立。"""
    e = rules["entry"]
    if _missing(row, "close", "sma20", "sma50", "sma200", "rsi14", "ret_60d"):
        return ["データ不足"]
    fails = []
    if e.get("require_trend") and not (row["close"] > row["sma50"] > row["sma200"]):
        fails.append("トレンド条件 (終値 > SMA50 > SMA200) を満たさない")
    if row["ret_60d"] < e.get("min_ret_60d", -math.inf):
        fails.append(f"60日リターン {row['ret_60d']:.1%} が基準未満")
    if row["rsi14"] >= e.get("max_rsi", math.inf):
        fails.append(f"RSI {row['rsi14']:.0f} が過熱")
    gap = row["close"] / row["sma20"] - 1
    if gap > e.get("max_gap_above_sma20", math.inf):
        fails.append(f"SMA20 から +{gap:.1%} 乖離")
    return fails


def exit_check(row: dict, avg_cost: float, peak_close: float, held_days: int,
               took_profit: bool, rules: dict) -> tuple[str, float] | None:
    """売りシグナル (理由, 売る割合) を返す。なければ None。"""
    x = rules["exit"]
    close = row["close"]
    ret = close / avg_cost - 1
    if x.get("stop_loss") and ret <= -x["stop_loss"]:
        return "stop_loss", 1.0
    if x.get("trailing_stop") and close <= peak_close * (1 - x["trailing_stop"]):
        return "trailing_stop", 1.0
    if x.get("trend_break") and not _missing(row, "sma50") \
            and close < row["sma50"] * (1 - x.get("trend_break_buffer", 0)):
        return "trend_break", 1.0
    if x.get("max_hold_days") and held_days >= x["max_hold_days"]:
        return "time_stop", 1.0
    if x.get("take_profit") and not took_profit and ret >= x["take_profit"]:
        return "take_profit", float(x.get("take_profit_fraction", 1.0))
    return None


def sell_qty(qty: int, fraction: float) -> int:
    return qty if fraction >= 1 else max(1, min(qty, int(qty * fraction)))
