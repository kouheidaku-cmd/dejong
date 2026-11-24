"""surge: 日本株も対象にする案 (markets: [USD, JPY]) と現行 ([USD]) の比較。手順は PROTOCOL.md。
実行: python reports/surge/2026-11-24-markets/markets.py [--no-fetch]
"""
import dataclasses
import json
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from surge import backtest as bt, data as sdata, strategy as sstrat  # noqa: E402
from surge.cli import load_config  # noqa: E402

OUT = Path(__file__).parent
cfg = load_config()
base, cost, mult = cfg["params"], cfg["execution"]["cost"], cfg["execution"]["max_weight_mult"]
panel = sdata.load_panel(refresh="--no-fetch" not in sys.argv)
VARIANTS = {"A_USD": ("USD",), "B_USD_JPY": ("USD", "JPY"), "ref_JPY": ("JPY",)}
PERIODS = {"2005-2012": ("2005-06-01", "2012-12-31"), "2013-2019": ("2013-01-01", "2019-12-31"), "2020-": ("2020-01-01", None)}


def summ(s):
    h = s["h4m"]
    return {"cagr": s["cagr"], "max_dd": s["max_drawdown"], "sharpe": s["sharpe"], "4m_median": h["median"],
            "4m_p_ge_20": h["p_ge_20"], "4m_p_lt_0": h["p_lt_0"], "4m_p05": h["p05"], "trades": s.get("trades")}


res = {"full": {}, "periods": {}, "robust": {}, "robust_mean": {}, "jp_share": {}}
for name, mk in VARIANTS.items():
    p = dataclasses.replace(base, markets=mk)
    r = bt.run(panel, p, start="2005-06-01", capital=400_000, cost=cost, integer=True, max_weight_mult=mult)
    res["full"][name] = summ(r.stats())
    res["full"][name]["period"] = [str(r.equity.index[0].date()), str(r.equity.index[-1].date())]
    res["full"][name]["yearly"] = {int(y): float(v) for y, v in bt.yearly_returns(r.equity).items()}
    t = r.trades
    if len(t) and "ticker" in t.columns:
        buys = t[t.get("side", pd.Series(["buy"] * len(t))) == "buy"] if "side" in t.columns else t
        res["jp_share"][name] = float(buys["ticker"].astype(str).str.endswith(".T").mean())
    res["periods"][name] = {}
    for pn, (s, e) in PERIODS.items():
        rr = bt.run(panel, p, start=s, end=e, capital=400_000, cost=cost, integer=True, max_weight_mult=mult)
        res["periods"][name][pn] = summ(rr.stats())
    rows = []
    for n in (4, 5, 6, 8):
        for lb in ((63, 126, 252), (126, 252), (63, 126), (21, 63, 126)):
            pp = dataclasses.replace(p, top_n=n, hold_rank=4 * n, lookbacks=lb)
            rows.append({"top_n": n, "lookbacks": "/".join(map(str, lb)),
                         **summ(bt.run(panel, pp, start="2005-06-01", cost=cost, integer=False).stats())})
    d = pd.DataFrame(rows)
    res["robust"][name] = d.round(4).to_dict(orient="records")
    res["robust_mean"][name] = d.drop(columns=["top_n", "lookbacks"]).mean().to_dict()
    print(name, "full", {k: round(v, 3) for k, v in res["full"][name].items() if isinstance(v, float)})
    print(name, "robust_mean", {k: round(v, 3) for k, v in res["robust_mean"][name].items()})
    for pn, v in res["periods"][name].items():
        print("  ", pn, f"cagr {v['cagr']:+.1%} dd {v['max_dd']:.1%}")

(OUT / "result.json").write_text(json.dumps(res, ensure_ascii=False, indent=2, default=float) + "\n", encoding="utf-8")
print("->", (OUT / "result.json").relative_to(ROOT))
