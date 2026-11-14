"""検証レポートの補足の数字: 開始時点の地合い別の「4か月後」、売買の回数、時期別の成績。
実行: python reports/surge/backtest/2026-11-13-full/conditional.py (キャッシュ済みのデータを使う)
"""
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[4]))
from surge import backtest, data, strategy  # noqa: E402
from surge.cli import load_config  # noqa: E402

cfg = load_config()
p = cfg["params"]
panel = data.load_panel(refresh=False)
feat = strategy.compute_features(panel, p)
r = backtest.run(panel, p, start="2005-06-01", cost=cfg["execution"]["cost"], feat=feat)
roll = backtest.rolling_returns(r.equity)
us = next(j for j, t in enumerate(panel.tickers) if not t.endswith(".T"))
on = pd.Series(feat.risk_on[:, us], index=panel.dates).reindex(roll.index)


def dist(x):
    return {"n": int(len(x)), "median": round(float(x.median()), 4), "p_ge_20": round(float((x >= .2).mean()), 4),
            "p_ge_10": round(float((x >= .1).mean()), 4), "p_lt_0": round(float((x < 0).mean()), 4),
            "p_le_m10": round(float((x <= -.1).mean()), 4), "p05": round(float(x.quantile(.05)), 4), "worst": round(float(x.min()), 4)}


years = len(r.equity) / backtest.YEAR
t = r.trades
out = {
    "all": dist(roll),
    "sp500_above_sma200_at_start": dist(roll[on]),
    "sp500_below_sma200_at_start": dist(roll[~on]),
    "round_trips_per_year": round(len(t) / years, 1),
    "avg_win": round(float(t.loc[t.ret > 0, "ret"].mean()), 4), "avg_loss": round(float(t.loc[t.ret <= 0, "ret"].mean()), 4),
    "exit_reasons": {k: int(v) for k, v in t.groupby("reason").size().items()},
    "median_holding_days": int((pd.to_datetime(t["exit"]) - pd.to_datetime(t["entry"])).dt.days.median()),
    "worst_drawdowns": {},
}
dd = r.equity / r.equity.cummax() - 1
for a, b in (("2005", "2012"), ("2013", "2019"), ("2020", "2026")):
    x = dd.loc[a:b]
    out["worst_drawdowns"][f"{a}-{b}"] = {"max_dd": round(float(-x.min()), 4), "bottom": str(x.idxmin().date())}
out["drawdown_now"] = round(float(dd.iloc[-1]), 4)
print(json.dumps(out, ensure_ascii=False, indent=2))
(Path(__file__).with_name("conditional.json")).write_text(json.dumps(out, ensure_ascii=False, indent=2) + "\n")
