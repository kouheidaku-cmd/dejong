"""2026-11-14 の検証の補足 (事前登録した判定には使わない)。

4つのルールを期間ごとに通しで走らせ、(1) 売買の回数、(2) 「どの日から始めても4か月後に何%か」、
(3) 母集団を同じ持ち高だけ均等に持った場合との比較、(4) 判定を現地通貨の価格で行った場合の違い、を出す。
実行: python reports/backtest/2026-11-14-supplement/supplement.py
"""
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from trader import backtest, cli, config, rules  # noqa: E402
from trader.data import CachedData, YahooData  # noqa: E402

RULES = {"v3": "config/rules.yaml", "s2": "rules/candidates/2026-11-13-s2-atr3.yaml",
         "w1": "rules/candidates/2026-10-24-w1-swap-gap20.yaml", "v4-draft": "rules/candidates/2026-11-14-v4-draft.yaml"}
PERIODS = {"holdout_2005_2018": ("2005-02-01", "2018-10-31"), "recent_2018_2026": ("2018-11-12", "2026-11-13")}
days = 23 * 365 + 300
data = CachedData(YahooData(), ROOT / "data" / "cache")
universe = config.load_universe(ROOT)
frames, settings, local = cli._load_frames(ROOT, data, universe, days, with_local=True)
kw = cli._run_kw(settings)


def four_months(e: pd.Series) -> dict:
    f = e.reindex(e.index + pd.Timedelta(days=122), method="ffill")
    f.index = e.index
    r = (f / e - 1)[e.index <= e.index[-1] - pd.Timedelta(days=122)]
    return {"median": round(float(r.median()), 4), "p_ge_20": round(float((r >= .2).mean()), 4),
            "p_ge_10": round(float((r >= .1).mean()), 4), "p_lt_0": round(float((r < 0).mean()), 4),
            "p_le_m10": round(float((r <= -.1).mean()), 4), "p05": round(float(r.quantile(.05)), 4), "worst": round(float(r.min()), 4)}


def curve_stats(e: pd.Series) -> dict:
    years = (e.index[-1] - e.index[0]).days / 365.25
    return {"total_return": round(float(e.iloc[-1] / e.iloc[0] - 1), 4), "cagr": round(float((e.iloc[-1] / e.iloc[0]) ** (1 / years) - 1), 4),
            "max_drawdown": round(float((1 - e / e.cummax()).max()), 4), "four_months": four_months(e)}


out = {}
for pname, (a, b) in PERIODS.items():
    a_ts, b_ts = pd.Timestamp(a), pd.Timestamp(b)
    bh = backtest.buy_and_hold(frames, a_ts, b_ts).pct_change().fillna(0)
    idx = cli.index_blend_curve(data, days, a_ts, b_ts).pct_change().fillna(0)
    out[pname] = {"period": [a, b], "rules": {}}
    for name, path in RULES.items():
        rs = rules.load_rules(ROOT / path)
        market = cli.market_regimes(data, frames, rs, settings, days)
        earnings = cli.earnings_for(data, frames, rs)
        for mode, sf in (("jpy_signals", None), ("local_signals", local)):
            if mode == "local_signals" and name not in ("v3", "v4-draft"):
                continue
            r = backtest.run(frames, rs, a_ts.date(), b_ts.date(), market=market, earnings=earnings, signal_frames=sf, **kw)
            e, m = r["equity_curve"]["equity"], r["metrics"]
            k = m["exposure"]
            row = {**curve_stats(e), "sharpe": m["sharpe"], "exposure": k, "trades": m["trades"], "win_rate": m["win_rate"],
                   "by_exit_reason": {x: v["trades"] for x, v in r["analysis"].get("by_exit_reason", {}).items()},
                   "universe_same_exposure": curve_stats((1 + k * bh).cumprod()),
                   "index_same_exposure": curve_stats((1 + k * idx).cumprod())}
            out[pname]["rules"][name if mode == "jpy_signals" else f"{name} (local signals)"] = row
            print(pname, name, mode, row["total_return"], row["max_drawdown"], row["trades"], flush=True)
    out[pname]["universe_full"] = curve_stats((1 + bh).cumprod())
    out[pname]["index_full"] = curve_stats((1 + idx).cumprod())
Path(__file__).with_name("supplement.json").write_text(json.dumps(out, ensure_ascii=False, indent=2) + "\n")
