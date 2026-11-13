"""config/rules.yaml の売買ルールの判定。バックテストと日次シグナルの両方がここを使う。"""
from __future__ import annotations

import math
from pathlib import Path

import numpy as np
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
        "sma5": c.rolling(5).mean(),
        "sma20": c.rolling(20).mean(),
        "sma50": c.rolling(50).mean(),
        "sma200": c.rolling(200).mean(),
        "rsi14": rsi(c),
        "ret_60d": c / c.shift(60) - 1,
        "mom_12_1": c.shift(21) / c.shift(252) - 1,  # 過去12か月 (直近1か月を除く) のリターン
        "mom_6_1": c.shift(21) / c.shift(126) - 1,
        "drop_120": 1 - c / c.rolling(120).max(),  # 直近120営業日の最高終値からの下落率 (0.2 = -20%)
        "atr14_pct": _atr_pct(df),
    }, index=df.index)


def _atr_pct(df: pd.DataFrame, n: int = 14) -> pd.Series:
    """ATR (直近 n 日の真の値幅の平均) を終値で割ったもの。高値・安値がなければ NaN。"""
    if not {"High", "Low"} <= set(df.columns):
        return pd.Series(np.nan, index=df.index)
    pc = df["Close"].shift()
    tr = pd.concat([df["High"] - df["Low"], (df["High"] - pc).abs(), (df["Low"] - pc).abs()], axis=1).max(axis=1)
    return tr.rolling(n).mean() / df["Close"]


def days_to_earnings(index: pd.DatetimeIndex, dates) -> pd.Series:
    """各日から次の決算日まで何営業日か (その銘柄の日足がある日で数える。決算日当日は 0)。

    決算日が休場日なら次の営業日を決算日とみなす。日足の最後より先の決算日は平日の数で数える。
    次の決算日が分からない日は NaN。
    """
    n = len(index)
    if n == 0:
        return pd.Series(dtype=float)
    pos = []
    for d in sorted(pd.Timestamp(x).normalize() for x in dates):
        i = int(index.searchsorted(d))
        if i >= n:
            i = n - 1 + int(np.busday_count(index[-1].date(), d.date()))
        pos.append(i)
    out = np.full(n, np.nan)
    if pos:
        pos = np.array(sorted(set(pos)))
        rows = np.arange(n)
        k = np.searchsorted(pos, rows)  # 各行以降で最初の決算日
        ok = k < len(pos)
        out[ok] = pos[k[ok]] - rows[ok]
    return pd.Series(out, index=index)


def days_since_earnings(index: pd.DatetimeIndex, dates) -> pd.Series:
    """各日が直近の決算日から何営業日後か (決算日当日は 0)。まだ決算がない日は NaN。

    決算日が休場日なら次の営業日を決算日とみなす (days_to_earnings と同じ)。
    """
    n = len(index)
    out = np.full(n, np.nan)
    pos = sorted({int(index.searchsorted(pd.Timestamp(x).normalize())) for x in dates})
    pos = np.array([i for i in pos if i < n])
    if len(pos):
        rows = np.arange(n)
        k = np.searchsorted(pos, rows, side="right") - 1  # 各行以前で最後の決算日
        ok = k >= 0
        out[ok] = rows[ok] - pos[k[ok]]
    return pd.Series(out, index=index)


def attach_earnings(f: pd.DataFrame, dates) -> pd.DataFrame:
    """銘柄の特徴量に、次の決算日までの営業日数 (dte) を付ける。"""
    f = f.copy()
    f["dte"] = days_to_earnings(f.index, dates)
    f["dse"] = days_since_earnings(f.index, dates)
    f["earn_id"] = np.arange(len(f)) + f["dte"]  # どの決算か (同じ決算なら日が進んでも同じ値)
    return f


def market_regime(index_df: pd.DataFrame, sma: int) -> pd.Series:
    """市場指数が上昇トレンドか (終値 > SMA)。その日の終値時点で分かる情報だけ。"""
    c = index_df["Close"]
    ma = c.rolling(sma).mean()
    return (c > ma).where(ma.notna())


def market_filter(rules: dict) -> dict | None:
    """ルールが使う市場フィルターの設定 (スイング型は entry、入れ替え型は rotation の下)。"""
    return (rules.get("entry") or {}).get("market_filter") or (rules.get("rotation") or {}).get("market_filter")


def rotation_targets(rows: dict[str, dict], rules: dict) -> list[str]:
    """月次入れ替え型: 各銘柄の最新の特徴量から、持つべき銘柄 (上位 top_n) を返す。"""
    rot = rules["rotation"]
    ranked = []
    for t, row in rows.items():
        v = row.get(rot["lookback"])
        if v is None or v != v:
            continue
        if rot.get("require_positive_momentum") and v <= 0:
            continue
        if rot.get("market_filter"):
            ok = row.get("market_ok")
            if ok is None or ok != ok or not ok:
                continue
        ranked.append((v, t))
    return [t for _, t in sorted(ranked, reverse=True)[:rot["top_n"]]]


def attach_market(f: pd.DataFrame, regime: pd.Series) -> pd.DataFrame:
    """銘柄の特徴量に、その日の市場指数の判定 (market_ok) を付ける。"""
    f = f.copy()
    f["market_ok"] = regime.reindex(f.index, method="ffill")
    return f


def _missing(row, *keys) -> bool:
    return any(row.get(k) is None or math.isnan(row[k]) for k in keys)


def rule_sets(rules: dict) -> list[tuple[str | None, dict, set[str] | None]]:
    """本体のルールと別枠 (sleeves) を (枠の名前, ルール, 対象銘柄) の並びで返す。本体は名前も対象銘柄も None。

    別枠は、決まった銘柄だけに本体と違う買い方・売り方を当てはめる (例: 急落後の反発を狙う押し目買い)。
    枠ごとに entry / exit / sizing を持ち、同時保有の数も枠ごとに数える。現金は本体と共通。
    earnings は書かなければ本体のものを使う。
    """
    out = [(None, rules, None)]
    for name, s in (rules.get("sleeves") or {}).items():
        out.append((name, {"earnings": rules.get("earnings"), **s}, {str(t) for t in s["tickers"]}))
    return out


def sleeve_tickers(rules: dict) -> list[str]:
    """別枠の対象銘柄 (重複なし、書いてある順)。"""
    return list(dict.fromkeys(str(t) for s in (rules.get("sleeves") or {}).values() for t in s["tickers"]))


def _trend_fails(row: dict, e: dict) -> list[str]:
    """順張り (既定): 上昇トレンドの銘柄を、過熱していないときに買う。"""
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


def _dip_fails(row: dict, e: dict) -> list[str]:
    """押し目買い (mode: dip): 直近の高値から大きく下げた銘柄を、下げ止まりを確認してから買う。"""
    need = ["close", "sma5", "drop_120"] + (["sma200"] if e.get("require_long_trend") else [])
    if _missing(row, *need):
        return ["データ不足"]
    fails = []
    if row["drop_120"] < e["min_drop"]:
        fails.append(f"高値からの下落 {row['drop_120']:.1%} が基準 {e['min_drop']:.0%} に届かない")
    if e.get("require_rebound") and not row["close"] > row["sma5"]:
        fails.append("下げ止まりを確認できない (終値 <= SMA5)")
    if e.get("require_long_trend") and not row["close"] > row["sma200"]:
        fails.append("長期トレンドが崩れている (終値 <= SMA200)")
    return fails


def entry_check(row: dict, rules: dict) -> list[str]:
    """新規買いの条件のうち満たしていないものを返す。空なら買い条件成立。"""
    e = rules["entry"]
    fails = _dip_fails(row, e) if e.get("mode") == "dip" else _trend_fails(row, e)
    if fails == ["データ不足"]:
        return fails
    mf = e.get("market_filter")
    if mf:
        ok = row.get("market_ok")
        if ok is None or ok != ok:  # NaN
            fails.append("市場指数のデータ不足")
        elif not ok:
            fails.append(f"市場全体が下降トレンド (指数 < SMA{mf['sma']})")
    blackout = (rules.get("earnings") or {}).get("entry_blackout_days")
    dte = row.get("dte")
    # 翌営業日に約定するので、約定日が決算日の blackout 営業日前〜当日になるシグナルを止める
    if blackout is not None and dte is not None and dte == dte and 1 <= dte <= blackout + 1:
        fails.append(f"決算日まで {int(dte)} 営業日 (決算前の新規買いは見送り)")
    after = (rules.get("earnings") or {}).get("entry_after_days")
    dse = row.get("dse")
    # 翌営業日に約定するので、約定日が決算日の翌営業日〜after 営業日後になるシグナルを止める
    if after and dse is not None and dse == dse and 0 <= dse <= after - 1:
        fails.append(f"決算日から {int(dse)} 営業日 (決算直後の新規買いは見送り)")
    return fails


def stop_pct(rules: dict, entry_atr_pct: float | None = None) -> float | None:
    """損切り幅。exit.stop_atr_mult があれば「買った日の ATR% × 倍率」、ATR が分からなければ exit.stop_loss。"""
    x = rules["exit"]
    k = x.get("stop_atr_mult")
    if k and entry_atr_pct is not None and entry_atr_pct == entry_atr_pct and entry_atr_pct > 0:
        return k * entry_atr_pct
    return x.get("stop_loss")


def exit_check(row: dict, avg_cost: float, peak_close: float, held_days: int,
               took_profit: bool, rules: dict, entry_atr_pct: float | None = None) -> tuple[str, float] | None:
    """売りシグナル (理由, 売る割合) を返す。なければ None。entry_atr_pct は買った日の ATR% (ATR 損切りのとき)。"""
    x = rules["exit"]
    close = row["close"]
    ret = close / avg_cost - 1
    stop = stop_pct(rules, entry_atr_pct)
    if stop and ret <= -stop:
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


def earnings_exit(row: dict, rules: dict) -> float | None:
    """決算前に売る割合を返す (翌営業日の寄付きが決算日の exit_before_days 営業日前以降になる日)。なければ None。"""
    e = rules.get("earnings") or {}
    k, dte = e.get("exit_before_days"), row.get("dte")
    if k is None or dte is None or dte != dte or not 1 <= dte <= k + 1:
        return None
    return float(e.get("exit_fraction", 1.0))


def sell_qty(qty: int, fraction: float) -> int:
    return qty if fraction >= 1 else max(1, min(qty, int(qty * fraction)))
