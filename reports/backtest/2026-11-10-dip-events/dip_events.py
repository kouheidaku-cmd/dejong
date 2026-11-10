"""前提の確認: 「AI 半導体株は、下がったあとは必ず上がる」は過去のデータでどのくらい正しいか。

仕様は rules/candidates/PROTOCOL-2026-11-10.md に、走らせる前に書いた。使い方 (リポジトリ直下で):
  python reports/backtest/2026-11-10-dip-events/dip_events.py
結果は同じフォルダの result.json に出る。
"""
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from trader import rules  # noqa: E402
from trader.data import YahooData  # noqa: E402

HORIZONS = (20, 60, 120, 250)   # 何営業日後のリターンを見るか
THRESHOLDS = (0.15, 0.25)       # 直近120営業日の最高終値からの下落率
MIN_GAP = 60                    # 同じ銘柄の「急落」は 60営業日以上あけて数える


def events(close: pd.Series, threshold: float) -> list[int]:
    """下落率が threshold 以上に「なった日」の位置 (前日は threshold 未満)。"""
    drop = (1 - close / close.rolling(120).max()).to_numpy()
    out, last = [], -MIN_GAP
    for i in range(1, len(drop)):
        if drop[i] >= threshold and drop[i - 1] < threshold and i - last >= MIN_GAP:
            out.append(i)
            last = i
    return out


def stats(xs: list[float]) -> dict:
    if not xs:
        return {"n": 0}
    a = np.array(xs)
    return {"n": len(a), "positive": round(float((a > 0).mean()), 3), "median": round(float(np.median(a)), 4),
            "p10": round(float(np.percentile(a, 10)), 4), "worst": round(float(a.min()), 4)}


def study(closes: dict[str, pd.Series], start=None, end=None) -> dict:
    """急落の日から見た先のリターンと、同じ銘柄・同じ期間の「いつ買っても」のリターン。"""
    out = {}
    for th in THRESHOLDS:
        fwd = {h: [] for h in HORIZONS}
        further, recovered, n_events, per_ticker = [], [], 0, {}
        for t, c in closes.items():
            v = c.to_numpy()
            high = c.rolling(120).max().to_numpy()
            idx = [i for i in events(c, th) if (start is None or c.index[i] >= start) and (end is None or c.index[i] <= end)]
            per_ticker[t] = len(idx)
            n_events += len(idx)
            for i in idx:
                for h in HORIZONS:
                    if i + h < len(v):
                        fwd[h].append(v[i + h] / v[i] - 1)
                if i + 120 < len(v):
                    further.append(v[i + 1:i + 121].min() / v[i] - 1)  # その後120営業日でさらにどこまで下げたか
                if i + 250 < len(v):
                    recovered.append(bool((v[i + 1:i + 251] >= high[i]).any()))  # 250営業日以内に元の高値を回復したか
        out[f"drop_{int(th * 100)}"] = {
            "events": n_events, "events_by_ticker": per_ticker,
            "forward_return": {str(h): stats(fwd[h]) for h in HORIZONS},
            "further_fall_within_120d": stats(further),
            "recovered_high_within_250d": round(float(np.mean(recovered)), 3) if recovered else None,
        }
    anyday = {h: [] for h in HORIZONS}
    for c in closes.values():
        c = c[(c.index >= start) if start is not None else slice(None)]
        c = c[(c.index <= end) if end is not None else slice(None)]
        for h in HORIZONS:
            anyday[h] += (c.shift(-h) / c - 1).dropna().tolist()
    out["any_day"] = {"forward_return": {str(h): stats(anyday[h]) for h in HORIZONS}}
    return out


def longest_underwater(close: pd.Series) -> dict:
    """最高値を更新できなかった最長の期間と、その間の最大の下落。"""
    peak = close.cummax()
    under = close < peak
    best = {"days": 0}
    start = None
    for d, u in under.items():
        if u and start is None:
            start = d
        if (not u or d == close.index[-1]) and start is not None:
            days = (d - start).days
            if days > best["days"]:
                seg = close[start:d]
                best = {"days": days, "years": round(days / 365.25, 1), "from": start.date().isoformat(),
                        "to": d.date().isoformat(), "recovered": bool(not u),
                        "max_fall": round(float(seg.min() / peak[start] - 1), 4)}
            start = None
    return best


def main():
    cand = rules.load_rules(ROOT / "rules" / "candidates" / "2026-11-10-d1-dip15-trend.yaml")
    tickers = rules.sleeve_tickers(cand)
    data = YahooData()
    hist = data.history(tickers, 9 * 365 + 300)  # バックテストと同じ期間の日足 (現地通貨)
    closes = {t: hist[t]["Close"] for t in tickers if t in hist}
    start = pd.Timestamp("2019-01-01")           # 年ごとの検証と同じ 2019 年から数える
    result = {"tickers": sorted(closes), "until": max(c.index[-1] for c in closes.values()).date().isoformat(),
              "ai_semis_2019_on": study(closes, start=start)}

    long_hist = data.history(["^SOX", "INTC", "NVDA"], 40 * 365)  # 取れるだけ昔から
    sox = long_hist["^SOX"]["Close"]
    result["sox_index"] = {
        "first_bar": sox.index[0].date().isoformat(),
        "longest_underwater": longest_underwater(sox),
        "before_2017": study({"^SOX": sox}, end=pd.Timestamp("2016-12-31")),
    }
    result["longest_underwater"] = {t: longest_underwater(long_hist[t]["Close"]) for t in ("INTC", "NVDA") if t in long_hist}
    out = Path(__file__).with_name("result.json")
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    json.dump(result, sys.stdout, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    main()
