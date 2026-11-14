"""surge と v3 を市場の状況で切り替えると良くなるかの確認 (2026-11-14)。

切り替えの合図は、走らせる前に決めた単純なもの (数字は定番の値)。結果の良いものを選ぶためではなく、
「切り替えという考え方に中身があるか」を見るために、合図ごとに数字を変えた版も全部出す。
- 2つの仕組みを 2005-06 から通しで仮想売買し、その日々の損益を合図で選ぶ
- 合図はその日の終値までで判定し、2営業日後から切り替える (発注に1日かかる)
- 切り替えのたびに資産の 0.5% を引く (片方を全部売って、もう片方を全部買うため)
実行: python reports/surge/2026-11-14-switch/switch.py
"""
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from surge import backtest as sbt, data as sdata, strategy as sstrat  # noqa: E402
from surge.cli import load_config  # noqa: E402
from trader import backtest as tbt, cli as tcli, config as tconfig, rules as trules  # noqa: E402
from trader.data import CachedData, YahooData  # noqa: E402

START, SWITCH_COST, LAG = "2005-06-01", 0.005, 2
OUT = Path(__file__).parent

# --- surge
cfg = load_config()
panel = sdata.load_panel(refresh="--no-fetch" not in sys.argv)
feat = sstrat.compute_features(panel, cfg["params"])
surge = sbt.run(panel, cfg["params"], start=START, capital=1_000_000, cost=cfg["execution"]["cost"], feat=feat).equity

# --- v3 (判定は現地通貨 = 日次と同じ)
days = 23 * 365 + 300
tdata = CachedData(YahooData(), ROOT / "data" / "cache")
frames, settings, local = tcli._load_frames(ROOT, tdata, tconfig.load_universe(ROOT), days, with_local=True)
rs = trules.load_rules(ROOT / "config" / "rules.yaml")
r = tbt.run(frames, rs, pd.Timestamp(START).date(), surge.index[-1].date(),
            market=tcli.market_regimes(tdata, frames, rs, settings, days), earnings=tcli.earnings_for(tdata, frames, rs),
            signal_frames=local, **tcli._run_kw(settings))
v3 = r["equity_curve"]["equity"].reindex(surge.index).ffill().bfill()

rs_, rv = surge.pct_change().fillna(0), v3.pct_change().fillna(0)
spx = pd.Series(panel.index_close["USD"], index=panel.dates)
spx_ret = spx.pct_change()


def sma(x, n):
    return x.rolling(n).mean()


signals = {}   # True = surge を使う
for n in (100, 200, 300):
    signals[f"S&P500 が {n}日線より上"] = (spx > sma(spx, n)).reindex(surge.index)
for n in (50, 100, 200):
    signals[f"surge の資産が自分の {n}日線より上"] = surge > sma(surge, n)
for dd in (0.10, 0.15, 0.20):
    signals[f"surge が最高値から {dd:.0%} 以内"] = surge / surge.cummax() - 1 > -dd
vol = (spx_ret.rolling(20).std() * np.sqrt(252)).reindex(surge.index)
for n in (126, 252, 504):
    signals[f"S&P500 の値動きが過去 {n}日の中央値より小さい"] = vol < vol.rolling(n).median()
for n in (63, 126, 252):
    signals[f"過去 {n}日のリターンが surge の方が上"] = surge.pct_change(n) > v3.pct_change(n)


def stats(e):
    years = (e.index[-1] - e.index[0]).days / 365.25
    d = e.pct_change().dropna()
    f = e.reindex(e.index + pd.Timedelta(days=122), method="ffill")
    f.index = e.index
    roll = (f / e - 1)[e.index <= e.index[-1] - pd.Timedelta(days=122)]
    return {"cagr": float((e.iloc[-1] / e.iloc[0]) ** (1 / years) - 1), "mdd": float((1 - e / e.cummax()).max()),
            "sharpe": float(d.mean() / d.std() * np.sqrt(len(d) / years)), "p20": float((roll >= .2).mean()),
            "p_lt0": float((roll < 0).mean()), "p05": float(roll.quantile(.05)), "median": float(roll.median())}


def sub(e, a, b):
    x = e.loc[a:b]
    return float((x.iloc[-1] / x.iloc[0]) ** (365.25 / (x.index[-1] - x.index[0]).days) - 1)


def switched(sig):
    use = sig.shift(LAG).fillna(False).astype(bool)
    ret = np.where(use, rs_, rv)
    flips = use.ne(use.shift()).fillna(False)
    flips.iloc[0] = False
    ret = ret - SWITCH_COST * flips.to_numpy()
    return (1 + pd.Series(ret, index=surge.index)).cumprod(), int(flips.sum()), float(use.mean())


rows = {}
for name, e, n, share in [("surge だけ", surge, 0, 1.0), ("v3 だけ", v3, 0, 0.0),
                          ("半分ずつ (毎日そろえる)", (1 + 0.5 * rs_ + 0.5 * rv).cumprod(), 0, 0.5)]:
    rows[name] = {**stats(e), "switches": n, "surge_share": share, "c0": sub(e, "2005", "2012"), "c1": sub(e, "2013", "2019"), "c2": sub(e, "2020", "2026")}
for name, sig in signals.items():
    e, n, share = switched(sig)
    rows[name] = {**stats(e), "switches": n, "surge_share": share, "c0": sub(e, "2005", "2012"), "c1": sub(e, "2013", "2019"), "c2": sub(e, "2020", "2026")}

print(f"{'':44s} 年率  最大下落 シャープ | 05-12  13-19  20-26 | 4か月: 中央値 +20%以上 マイナス 悪い方5% | 切替 surge の割合")
for name, s in rows.items():
    print(f"{name:40s} {s['cagr']:+6.1%} {s['mdd']:6.1%} {s['sharpe']:6.2f} | {s['c0']:+6.1%} {s['c1']:+6.1%} {s['c2']:+6.1%} | "
          f"{s['median']:+6.1%} {s['p20']:5.0%} {s['p_lt0']:6.0%} {s['p05']:+7.1%} | {s['switches']:4d} {s['surge_share']:5.0%}")

# --- 「今がその時か」: いまの状態と同じ日から surge を始めた場合の4か月後
f = surge.reindex(surge.index + pd.Timedelta(days=122), method="ffill")
f.index = surge.index
fwd = (f / surge - 1)[surge.index <= surge.index[-1] - pd.Timedelta(days=122)]
fwd_v3 = v3.reindex(v3.index + pd.Timedelta(days=122), method="ffill")
fwd_v3.index = v3.index
fwd_v3 = (fwd_v3 / v3 - 1).reindex(fwd.index)
now = {}
print("\nいまの状態 (最新の日) と、同じ状態の日から始めた surge の4か月後")
for name, sig in signals.items():
    cur = bool(sig.iloc[-1])
    same, other = fwd[sig.reindex(fwd.index) == cur], fwd[sig.reindex(fwd.index) != cur]
    v_same = fwd_v3[sig.reindex(fwd.index) == cur]
    now[name] = {"now": cur, "n": int(len(same)), "median": float(same.median()), "p20": float((same >= .2).mean()),
                 "p_lt0": float((same < 0).mean()), "p05": float(same.quantile(.05)),
                 "other_median": float(other.median()), "other_p20": float((other >= .2).mean()), "other_p_lt0": float((other < 0).mean()),
                 "v3_median": float(v_same.median()), "v3_p20": float((v_same >= .2).mean()), "v3_p_lt0": float((v_same < 0).mean())}
    x = now[name]
    print(f"{name:40s} いま: {'はい' if cur else 'いいえ':4s} | 同じ状態 ({x['n']:4d}日): 中央値 {x['median']:+5.1%} +20%以上 {x['p20']:4.0%} マイナス {x['p_lt0']:4.0%} 悪い方5% {x['p05']:+6.1%}"
          f" | 逆の状態: 中央値 {x['other_median']:+5.1%} +20%以上 {x['other_p20']:4.0%} マイナス {x['other_p_lt0']:4.0%} | 同じ状態の v3: 中央値 {x['v3_median']:+5.1%} +20%以上 {x['v3_p20']:3.0%}")
print(f"\n最新の日 {surge.index[-1].date()}: surge は最高値から {surge.iloc[-1] / surge.cummax().iloc[-1] - 1:+.1%}、"
      f"S&P500 は 200日線から {spx.iloc[-1] / sma(spx, 200).iloc[-1] - 1:+.1%}、S&P500 の値動き (20日、年率) {vol.iloc[-1]:.0%}")
(OUT / "result.json").write_text(json.dumps({"start": START, "until": str(surge.index[-1].date()), "switch_cost": SWITCH_COST, "lag_days": LAG,
                                             "systems_and_switches": rows, "now": now}, ensure_ascii=False, indent=2) + "\n")
pd.DataFrame({"surge": surge.round(0), "v3": v3.round(0)}).to_csv(OUT / "curves.csv")
