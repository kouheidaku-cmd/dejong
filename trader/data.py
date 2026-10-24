"""日足データの取得。

MarketData を満たすクラスなら差し替え可能 (テストでは合成データを使う)。
返す DataFrame は Open/High/Low/Close/Volume 列、日付 (tz なし) のインデックス。
"""
from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
from typing import Protocol

import pandas as pd

COLUMNS = ["Open", "High", "Low", "Close", "Volume"]


class MarketData(Protocol):
    def history(self, tickers: list[str], days: int) -> dict[str, pd.DataFrame]: ...


class YahooData:
    """yfinance 経由で Yahoo Finance の日足を取る。"""

    def history(self, tickers: list[str], days: int) -> dict[str, pd.DataFrame]:
        import yfinance as yf

        tickers = sorted(set(tickers))
        if not tickers:
            return {}
        raw = yf.download(
            tickers,
            start=(date.today() - timedelta(days=days)).isoformat(),
            interval="1d",
            group_by="ticker",
            auto_adjust=False,
            progress=False,
            threads=True,
        )
        out = {}
        for t in tickers:
            df = _select(raw, t)
            if df is not None and not df.empty:
                out[t] = df
        return out


    def earnings_dates(self, tickers: list[str]) -> dict[str, list[date]]:
        """過去と予定の決算発表日 (取引所の現地の日付)。取れなかった銘柄は含めない。"""
        import yfinance as yf

        out = {}
        for t in sorted(set(tickers)):
            try:
                df = yf.Ticker(t).get_earnings_dates(limit=60)
            except Exception:
                continue
            if df is None or df.empty:
                continue
            idx = pd.DatetimeIndex(df.index)
            if idx.tz is not None:
                idx = idx.tz_convert("Asia/Tokyo" if t.endswith(".T") else "America/New_York")
            out[t] = sorted({d.date() for d in idx})
        return out


def _select(raw: pd.DataFrame, ticker: str) -> pd.DataFrame | None:
    if isinstance(raw.columns, pd.MultiIndex):
        if ticker in raw.columns.get_level_values(0):
            df = raw[ticker]
        elif ticker in raw.columns.get_level_values(1):
            df = raw.xs(ticker, axis=1, level=1)
        else:
            return None
    else:
        df = raw
    df = df[COLUMNS].dropna(subset=["Close"]).copy()
    idx = pd.DatetimeIndex(df.index)
    if idx.tz is not None:
        idx = idx.tz_localize(None)
    df.index = idx.normalize()
    return df


def last_closes(data: dict[str, pd.DataFrame]) -> dict[str, float]:
    return {t: float(df["Close"].iloc[-1]) for t, df in data.items() if not df.empty}


def fx_rates(data: MarketData, tickers, settings: dict) -> dict[str, float]:
    """通貨ごとの「1単位 = 基準通貨でいくら」。基準通貨自身は 1.0。"""
    from .config import currency

    base = settings["base_currency"]
    need = {currency(t, settings) for t in tickers} - {base}
    rates = {base: 1.0}
    if not need:
        return rates
    pairs = {c: settings["fx_tickers"][c] for c in need}
    hist = data.history(sorted(pairs.values()), 10)
    for c, sym in pairs.items():
        if sym not in hist:
            raise RuntimeError(f"為替レート {sym} を取得できませんでした")
        rates[c] = float(hist[sym]["Close"].iloc[-1])
    return rates


class CachedData:
    """取得した日足を data/cache/ に CSV で保存し、同じ日のうちは再利用する (バックテストの繰り返し用)。"""

    def __init__(self, inner: MarketData, cache_dir: Path):
        self.inner = inner
        self.dir = cache_dir

    def history(self, tickers: list[str], days: int) -> dict[str, pd.DataFrame]:
        start = pd.Timestamp(date.today() - timedelta(days=days))
        out, missing = {}, []
        for t in tickers:
            path = self.dir / f"{t}.csv"
            if path.exists() and date.fromtimestamp(path.stat().st_mtime) == date.today():
                df = pd.read_csv(path, index_col=0, parse_dates=True)
                if not df.empty and df.index[0] <= start + pd.Timedelta(days=7):
                    out[t] = df[df.index >= start]
                    continue
            missing.append(t)
        if missing:
            fetched = self.inner.history(missing, days)
            self.dir.mkdir(parents=True, exist_ok=True)
            for t, df in fetched.items():
                df.to_csv(self.dir / f"{t}.csv")
            out.update(fetched)
        return out

    def earnings_dates(self, tickers: list[str]) -> dict[str, list[date]]:
        """決算日を data/cache/earnings/ に保存し、同じ日のうちは再利用する。"""
        d = self.dir / "earnings"
        out, missing = {}, []
        for t in tickers:
            path = d / f"{t}.csv"
            if path.exists() and date.fromtimestamp(path.stat().st_mtime) == date.today():
                out[t] = [date.fromisoformat(x) for x in path.read_text().split()]
            else:
                missing.append(t)
        if missing and hasattr(self.inner, "earnings_dates"):
            fetched = self.inner.earnings_dates(missing)
            d.mkdir(parents=True, exist_ok=True)
            for t, ds in fetched.items():
                (d / f"{t}.csv").write_text("\n".join(x.isoformat() for x in ds))
            out.update(fetched)
        return out
