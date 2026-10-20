"""テクニカル指標とスクリーニング用スコア。判断材料であって売買シグナルそのものではない。"""
from __future__ import annotations

import math

import pandas as pd


def rsi(close: pd.Series, n: int = 14) -> pd.Series:
    delta = close.diff()
    up = delta.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    down = (-delta.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    return 100 - 100 / (1 + up / down)


def summarize(df: pd.DataFrame) -> dict:
    c, h, l = df["Close"], df["High"], df["Low"]
    n = len(c)

    def ret(k):
        return c.iloc[-1] / c.iloc[-1 - k] - 1 if n > k else None

    def sma(k):
        return c.tail(k).mean() if n >= k else None

    prev = c.shift(1)
    tr = pd.concat([h - l, (h - prev).abs(), (l - prev).abs()], axis=1).max(axis=1)
    vol20 = c.pct_change().tail(20).std() * math.sqrt(252) if n > 20 else None
    volume_avg = df["Volume"].tail(20).mean()

    out = {
        "close": c.iloc[-1],
        "ret_1d": ret(1),
        "ret_5d": ret(5),
        "ret_20d": ret(20),
        "ret_60d": ret(60),
        "sma20": sma(20),
        "sma50": sma(50),
        "sma200": sma(200),
        "rsi14": rsi(c).iloc[-1] if n > 14 else None,
        "atr14_pct": tr.tail(14).mean() / c.iloc[-1] if n > 14 else None,
        "vol20_annual": vol20,
        "gap_from_52w_high": c.iloc[-1] / c.tail(250).max() - 1,
        "volume_ratio": df["Volume"].iloc[-1] / volume_avg if volume_avg else None,
    }
    return {"date": df.index[-1].date().isoformat(), **{k: _clean(v) for k, v in out.items()}}


def trend_ok(s: dict) -> bool:
    if s["sma50"] is None or s["close"] < s["sma50"]:
        return False
    return s["sma200"] is None or s["sma50"] > s["sma200"]


def screen(summaries: dict[str, dict]) -> list[dict]:
    """モメンタム中心の合成スコアで順位付けする (60日 50%, 20日 30%, 低ボラ 20%)。"""
    rows = [{"ticker": t, **s} for t, s in summaries.items() if s.get("ret_60d") is not None]
    if not rows:
        return []
    df = pd.DataFrame(rows).set_index("ticker")
    score = (
        0.5 * df["ret_60d"].rank(pct=True)
        + 0.3 * df["ret_20d"].rank(pct=True)
        + 0.2 * (-df["vol20_annual"]).rank(pct=True)
    )
    ranked = []
    for t in score.sort_values(ascending=False).index:
        s = summaries[t]
        ranked.append({"ticker": t, "score": round(float(score[t]), 3), "trend_ok": trend_ok(s), **s})
    return ranked


def _clean(v):
    if v is None:
        return None
    v = float(v)
    if math.isnan(v) or math.isinf(v):
        return None
    return round(v, 4)
