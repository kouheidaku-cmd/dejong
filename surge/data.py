"""日足データの取得と、検証・日次判断の両方が使う価格パネル。

Panel は「日付 × 銘柄」の表をまとめたもの。日付は日米どちらかの市場が開いた日の和集合で、
休場の日は終値を前の日の値で埋める (評価用)。始値は埋めない (その日に売買できないことを表す)。
価格は配当込みの調整後 (Adj Close) を使うので、配当は再投資した扱いになる。
"""
from __future__ import annotations

import sys
import time
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

ROOT = Path(__file__).resolve().parent.parent
CACHE = ROOT / "data" / "cache" / "surge"
FETCH_START = "2003-06-01"   # リーマンショック (2008年) を検証に含めるため
RAW_COLUMNS = ["Open", "High", "Low", "Close", "Adj Close", "Volume"]


def load_universe(path: Path | None = None) -> dict:
    u = yaml.safe_load((path or Path(__file__).with_name("universe.yaml")).read_text())
    group_of = {t: g for g, ts in u["groups"].items() for t in ts}
    return {"tickers": sorted(group_of), "group_of": group_of, "indices": u["indices"], "fx": u["fx"]}


def currency(ticker: str) -> str:
    return "JPY" if ticker.endswith(".T") else "USD"


# ---------------------------------------------------------------- 取得とキャッシュ

def fetch(tickers: list[str], start: str = FETCH_START, cache: Path = CACHE, force: bool = False) -> list[str]:
    """Yahoo Finance から日足を取り、銘柄ごとの CSV に保存する。直近1時間に取得済みなら取り直さない。取れなかった銘柄を返す。"""
    import yfinance as yf

    cache.mkdir(parents=True, exist_ok=True)
    todo = [t for t in sorted(set(tickers)) if force or not _fresh(cache / f"{t}.csv")]
    failed = []
    for i in range(0, len(todo), 40):
        batch = todo[i:i + 40]
        raw = yf.download(batch, start=start, interval="1d", group_by="ticker", auto_adjust=False,
                          progress=False, threads=True)
        for t in batch:
            df = _select(raw, t)
            if df is None or len(df) < 30:
                failed.append(t)
                continue
            drop_incomplete(df, t).to_csv(cache / f"{t}.csv")
    return failed


MARKET_CLOSE = {"JPY": ("Asia/Tokyo", 15, 50), "USD": ("America/New_York", 16, 20)}  # 終値が確定したとみなす現地時刻


def drop_incomplete(df: pd.DataFrame, ticker: str, now: pd.Timestamp | None = None) -> pd.DataFrame:
    """取引時間中に取った「今日の足」は終値が確定していないので落とす (為替は対象外)。"""
    if df.empty or ticker.endswith("=X"):
        return df
    tz, hh, mm = MARKET_CLOSE["JPY" if ticker.endswith(".T") or ticker == "^N225" else "USD"]
    now = (now or pd.Timestamp.now(tz="UTC")).tz_convert(tz)
    if df.index[-1].date() == now.date() and (now.hour, now.minute) < (hh, mm):
        return df.iloc[:-1]
    return df


def _fresh(path: Path, minutes: int = 60) -> bool:
    """直近 minutes 分以内に取得したファイルは取り直さない (続けて何度か走らせるときのため)。"""
    return path.exists() and time.time() - path.stat().st_mtime < minutes * 60


def _select(raw: pd.DataFrame, ticker: str) -> pd.DataFrame | None:
    if isinstance(raw.columns, pd.MultiIndex):
        if ticker not in raw.columns.get_level_values(0):
            return None
        df = raw[ticker]
    else:
        df = raw
    if not set(RAW_COLUMNS) <= set(df.columns):
        return None
    df = df[RAW_COLUMNS].dropna(subset=["Close"]).copy()
    idx = pd.DatetimeIndex(df.index)
    if idx.tz is not None:
        idx = idx.tz_localize(None)
    df.index = idx.normalize()
    df.index.name = "Date"
    return df[~df.index.duplicated(keep="last")]


def read_cache(tickers: list[str], cache: Path = CACHE) -> dict[str, pd.DataFrame]:
    out = {}
    for t in tickers:
        p = cache / f"{t}.csv"
        if p.exists():
            out[t] = pd.read_csv(p, index_col=0, parse_dates=True)
    return out


# ---------------------------------------------------------------- パネル

@dataclass
class Panel:
    dates: pd.DatetimeIndex
    tickers: list[str]
    close: np.ndarray       # 調整後終値 (現地通貨)、休場日は前の値
    open: np.ndarray        # 調整後始値 (現地通貨)、休場日は NaN
    raw_close: np.ndarray   # 画面に出る終値 (配当調整なし)。注文案の株数計算に使う
    dollar_vol: np.ndarray  # 売買代金 (円換算) の60日平均
    fx: np.ndarray          # 銘柄ごとの「1通貨 = 何円」(日付 × 銘柄)
    index_close: dict[str, np.ndarray]  # 通貨 → その市場の指数 (休場日は前の値)
    group_of: dict[str, str]

    def col(self, ticker: str) -> int:
        return self.tickers.index(ticker)

    def upto(self, n: int) -> "Panel":
        """先頭 n 日分だけのパネル (検証で「その日までしか見えない」ことを確かめるのに使う)。"""
        return Panel(self.dates[:n], self.tickers, self.close[:n], self.open[:n], self.raw_close[:n],
                     self.dollar_vol[:n], self.fx[:n], {k: v[:n] for k, v in self.index_close.items()}, self.group_of)


def build_panel(frames: dict[str, pd.DataFrame], universe: dict, start: str | None = None) -> Panel:
    """銘柄ごとの日足 (RAW_COLUMNS) からパネルを作る。frames には指数と為替も含める。"""
    tickers = [t for t in universe["tickers"] if t in frames]
    cal = sorted(set().union(*[frames[t].index for t in tickers]))
    dates = pd.DatetimeIndex(cal)
    if start:
        dates = dates[dates >= pd.Timestamp(start)]

    def col(t, name, ffill):
        s = frames[t][name].reindex(dates.union(frames[t].index)).sort_index()
        return (s.ffill() if ffill else s).reindex(dates).to_numpy(float)

    n, m = len(dates), len(tickers)
    close, open_, raw, vol = (np.full((n, m), np.nan) for _ in range(4))
    for j, t in enumerate(tickers):
        f = frames[t]
        factor = (f["Adj Close"] / f["Close"]).where(f["Close"] > 0)
        close[:, j] = col(t, "Adj Close", True)
        raw[:, j] = col(t, "Close", True)
        open_[:, j] = (f["Open"] * factor).reindex(dates).to_numpy(float)
        vol[:, j] = (f["Close"] * f["Volume"]).reindex(dates).fillna(0.0).to_numpy(float)

    fx = np.ones((n, m))
    for cur, sym in universe["fx"].items():
        rate = frames[sym]["Close"].reindex(dates.union(frames[sym].index)).sort_index().ffill().reindex(dates).to_numpy(float)
        for j, t in enumerate(tickers):
            if currency(t) == cur:
                fx[:, j] = rate
    dv = pd.DataFrame(vol * fx).rolling(60, min_periods=20).mean().to_numpy()

    index_close = {}
    for cur, sym in universe["indices"].items():
        s = frames[sym]["Close"]
        index_close[cur] = s.reindex(dates.union(s.index)).sort_index().ffill().reindex(dates).to_numpy(float)
    return Panel(dates, tickers, close, open_, raw, dv, fx, index_close, universe["group_of"])


def load_panel(universe: dict | None = None, start: str | None = None, refresh: bool = True,
               fetch_start: str = FETCH_START) -> Panel:
    universe = universe or load_universe()
    extra = list(universe["indices"].values()) + list(universe["fx"].values())
    names = universe["tickers"] + extra
    if refresh:
        failed = fetch(names, start=fetch_start)
        missing = [t for t in failed if t in extra]
        if missing:
            raise RuntimeError(f"指数・為替を取得できませんでした: {missing}")
        if failed:
            print(f"取得できなかった銘柄 (前回のデータがあればそれを使う): {', '.join(failed)}", file=sys.stderr)
    frames = read_cache(names)
    return build_panel(frames, universe, start=start)


def days_ago(n: int) -> str:
    return (date.today() - timedelta(days=n)).isoformat()
