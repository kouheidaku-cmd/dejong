"""基準と検証方法そのものの点検 (2026-11-10)。新しい売買ルールの候補を作るためのものではない。

使い方 (リポジトリ直下で): python reports/backtest/2026-11-10-review/review.py [continuous|bootstrap|phase|afford|costs|d1]
結果は同じフォルダの <名前>.json に出る。内容は reports/rules/2026-11-10-review.md。
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

OUT = Path(__file__).parent
DATA = CachedData(YahooData(), ROOT / "data" / "cache")
DAYS = 9 * 365 + 300
D1 = ROOT / "rules" / "candidates" / "2026-11-10-d1-dip15-trend.yaml"


def setup(rule_path):
    rule_set = rules.load_rules(rule_path)
    universe = config.load_universe(ROOT)
    frames, settings = cli._load_frames(ROOT, DATA, cli._with_sleeves(universe, [rule_set]), DAYS)
    frames, main = cli._scope(frames, universe, rule_set)
    return {"rules": rule_set, "frames": frames, "main": main, "settings": settings,
            "market": cli.market_regimes(DATA, frames, rule_set, settings, DAYS),
            "earnings": cli.earnings_for(DATA, frames, rule_set)}


def run(s, a, b, cash=1_000_000, halt=None, slippage_bps=10):
    """halt: 最高値からこの割合以上下げている間は新規買いを止める (settings.yaml の max_drawdown_pct と同じ動き)。"""
    orig, state = backtest._swing_orders, {"peak": 0.0, "halt_days": 0}

    def gated(d, feats, pos, orders, equity, rule_set, main_tickers=None):
        state["peak"] = max(state["peak"], equity)
        new = orig(d, feats, pos, orders, equity, rule_set, main_tickers)
        if halt and 1 - equity / state["peak"] >= halt:
            state["halt_days"] += 1
            new = [o for o in new if o["side"] == "sell"]
        return new

    backtest._swing_orders = gated
    try:
        r = backtest.run(s["frames"], s["rules"], a.date(), b.date(), initial_cash=cash, slippage_bps=slippage_bps,
                         market=s["market"], earnings=s["earnings"], main_tickers=s["main"])
    finally:
        backtest._swing_orders = orig
    r["halt_days"] = state["halt_days"]
    return r


def stats(curve: pd.Series) -> dict:
    """資産推移 (日次) からリターン・値動き・シャープレシオ・最大下落。"""
    r = curve.pct_change().dropna()
    years = (curve.index[-1] - curve.index[0]).days / 365.25
    return {"total_return": round(float(curve.iloc[-1] / curve.iloc[0] - 1), 4),
            "cagr": round(float((curve.iloc[-1] / curve.iloc[0]) ** (1 / years) - 1), 4),
            "vol": round(float(r.std() * np.sqrt(252)), 4),
            "sharpe": round(float(r.mean() / r.std() * np.sqrt(252)), 3),
            "max_drawdown": round(float((1 - curve / curve.cummax()).max()), 4)}


def by_year(curve: pd.Series) -> dict:
    out = {}
    for y, c in curve.groupby(curve.index.year):
        prev = curve[curve.index < c.index[0]]
        base = prev.iloc[-1] if len(prev) else c.iloc[0]
        out[int(y)] = {"return": round(float(c.iloc[-1] / base - 1), 4),
                       "max_drawdown": round(float((1 - c / c.cummax()).max()), 4)}
    return out


def scaled(curve: pd.Series, k: float) -> pd.Series:
    """資産の k 倍分だけを curve に入れ、残りは現金 (利息0) で持つ。毎日その比率に戻す。"""
    return (1 + k * curve.pct_change().fillna(0)).cumprod()


def continuous():
    """1. 8年を通しで走らせる: v3 と指数の比べ方、口座の大きさ、ドローダウン停止の影響。"""
    s = setup(ROOT / "config" / "rules.yaml")
    base = list(s["frames"].values())
    first = min(df.index[0] for df in base) + pd.Timedelta(days=300 + 365)  # 分割バックテストと同じ始点
    last = max(df.index[-1] for df in base)
    idx = cli.index_blend_curve(DATA, DAYS, first, last)
    out = {"period": [first.date().isoformat(), last.date().isoformat()], "runs": {}}
    curves = {}
    for name, cash, halt in [("v3_100万", 1_000_000, None), ("v3_100万_停止15%", 1_000_000, 0.15),
                             ("v3_40万", 400_000, None), ("v3_40万_停止15%", 400_000, 0.15)]:
        r = run(s, first, last, cash, halt)
        eq = r["equity_curve"]["equity"]
        curves[name] = eq
        m = r["metrics"]
        out["runs"][name] = {**stats(eq), "exposure": m["exposure"], "trades": m["trades"], "win_rate": m["win_rate"],
                             "profit_factor": m["profit_factor"], "halt_days": r["halt_days"], "days": len(eq),
                             "by_year": by_year(eq)}
    k = out["runs"]["v3_100万"]["exposure"]
    out["index_50_50"] = {**stats(idx), "by_year": by_year(idx)}
    out[f"index_at_v3_exposure"] = {"k": k, **stats(scaled(idx, k)), "by_year": by_year(scaled(idx, k))}
    bh = backtest.buy_and_hold(s["frames"], first, last)
    out["universe_buy_and_hold"] = stats(bh)
    # 「指数を k 倍 + 残り現金」が4基準を満たすか (年ごとに独立。指数の年ごとの数字は yearly.json と同じ計算)
    y = json.loads((ROOT / "reports" / "backtest" / "2026-11-10-dip-yearly" / "yearly.json").read_text())
    out["no_skill_vs_4_criteria"] = {}
    for kk in (0.5, 0.7, 0.9):
        rets, dds = [], []
        for yr in y["index_50_50"]:
            a, b = pd.Timestamp(int(yr), 1, 1), min(pd.Timestamp(int(yr), 12, 31), pd.Timestamp(y["until"]))
            c = scaled(cli.index_blend_curve(DATA, DAYS, a, b), kk)
            rets.append(c.iloc[-1] - 1)
            dds.append((1 - c / c.cummax()).max())
        out["no_skill_vs_4_criteria"][f"index_{int(kk * 100)}pct"] = {
            "positive_years": int(sum(r > 0 for r in rets)), "worst_year": round(float(min(rets)), 4),
            "chained_return": round(float(np.prod([1 + r for r in rets]) - 1), 4),
            "avg_max_drawdown": round(float(np.mean(dds)), 4)}
    (OUT / "continuous.json").write_text(json.dumps(out, ensure_ascii=False, indent=2) + "\n")
    pd.DataFrame({**curves, "index_50_50": idx}).to_csv(OUT / "continuous_curves.csv")
    return out


def _boot(a: np.ndarray, b: np.ndarray, reps=4000, block=20, seed=0):
    """対応のあるブロック・ブートストラップ: 2つの日次リターンのシャープレシオの差の散らばり。"""
    rng = np.random.default_rng(seed)
    n = len(a)
    sh = lambda x: x.mean() / x.std() * np.sqrt(252)
    diffs, s_a = [], []
    for _ in range(reps):
        idx = []
        while len(idx) < n:
            st = rng.integers(n)
            idx.extend((st + np.arange(rng.geometric(1 / block))) % n)
        idx = np.array(idx[:n])
        diffs.append(sh(b[idx]) - sh(a[idx]))
        s_a.append(sh(a[idx]))
    diffs, s_a = np.array(diffs), np.array(s_a)
    return {"sharpe_a": round(float(sh(a)), 3), "sharpe_b": round(float(sh(b)), 3), "diff": round(float(sh(b) - sh(a)), 3),
            "diff_ci90": [round(float(np.percentile(diffs, 5)), 2), round(float(np.percentile(diffs, 95)), 2)],
            "prob_b_better": round(float((diffs > 0).mean()), 3),
            "sharpe_a_ci90": [round(float(np.percentile(s_a, 5)), 2), round(float(np.percentile(s_a, 95)), 2)], "days": n}


def bootstrap():
    """2. 「シャープレシオが現行以上」という基準は、どのくらい偶然で決まるか。"""
    bt = ROOT / "reports" / "backtest"
    pairs = [("v2 → v3 (採用された変更)", "2026-10-24-v2-split", "2026-10-24-e1-entry-blackout-split"),
             ("v3 → a1 (決算翌日は買わない)", "2026-10-31-v3-split", "2026-10-31-a1-after1-split"),
             ("v3 → a3 (決算後5日は買わない)", "2026-10-31-v3-split", "2026-10-31-a3-after5-split"),
             ("v3 → d1 (押し目買い)", "2026-11-10-v3-split", "2026-11-10-d1-dip15-trend-split"),
             ("v3 → d2", "2026-11-10-v3-split", "2026-11-10-d2-dip25-split"),
             ("v3 → d3", "2026-11-10-v3-split", "2026-11-10-d3-dip15-split")]
    out = {}
    for label, a, b in pairs:
        out[label] = {}
        for part in ("in_sample", "out_of_sample"):
            ea = pd.read_csv(bt / a / f"equity_{part}.csv", index_col=0, parse_dates=True)["equity"]
            eb = pd.read_csv(bt / b / f"equity_{part}.csv", index_col=0, parse_dates=True)["equity"]
            df = pd.concat([ea.pct_change(), eb.pct_change()], axis=1).dropna()
            out[label][part] = _boot(df.iloc[:, 0].to_numpy(), df.iloc[:, 1].to_numpy())
    # v3 と指数 50/50 (同じ期間)
    out["v3 → 指数 50/50"] = {}
    for part in ("in_sample", "out_of_sample"):
        ea = pd.read_csv(bt / "2026-11-10-v3-split" / f"equity_{part}.csv", index_col=0, parse_dates=True)["equity"]
        idx = cli.index_blend_curve(DATA, DAYS, ea.index[0], ea.index[-1]).reindex(ea.index).ffill()
        df = pd.concat([ea.pct_change(), idx.pct_change()], axis=1).dropna()
        out["v3 → 指数 50/50"][part] = _boot(df.iloc[:, 0].to_numpy(), df.iloc[:, 1].to_numpy())
    (OUT / "bootstrap.json").write_text(json.dumps(out, ensure_ascii=False, indent=2) + "\n")
    return out


def phase():
    """3. 「年ごと」の区切りを1月始まりからずらすと、4基準の合否は変わるか (v3 と d1)。"""
    sets = {"v3": setup(ROOT / "config" / "rules.yaml"), "d1": setup(D1)}
    last = max(df.index[-1] for df in sets["v3"]["frames"].values())
    out = {}
    for m in range(1, 13):
        windows = []
        y = 2019
        while pd.Timestamp(y, m, 1) + pd.DateOffset(years=1) - pd.Timedelta(days=1) <= last:
            a = pd.Timestamp(y, m, 1)
            windows.append((a, a + pd.DateOffset(years=1) - pd.Timedelta(days=1)))
            y += 1
        row = {"windows": len(windows)}
        idx = [cli.index_blend_curve(DATA, DAYS, a, b) for a, b in windows]
        ir = [float(c.iloc[-1] - 1) for c in idx]
        row["index"] = {"positive": int(sum(r > 0 for r in ir)), "worst": round(min(ir), 4),
                        "chained": round(float(np.prod([1 + r for r in ir]) - 1), 4),
                        "avg_dd": round(float(np.mean([(1 - c / c.cummax()).max() for c in idx])), 4)}
        for name, s in sets.items():
            ms = [run(s, a, b)["metrics"] for a, b in windows]
            rets = [x["total_return"] for x in ms]
            row[name] = {"positive": int(sum(r > 0 for r in rets)), "worst": round(min(rets), 4),
                         "chained": round(float(np.prod([1 + r for r in rets]) - 1), 4),
                         "avg_dd": round(float(np.mean([x["max_drawdown"] for x in ms])), 4)}
        out[f"{m}月始まり"] = row
        print(m, row, file=sys.stderr, flush=True)
    (OUT / "phase.json").write_text(json.dumps(out, ensure_ascii=False, indent=2) + "\n")
    return out


def afford():
    """4. 実際の口座の大きさで、母集団のうち何銘柄が買えるか (最新の終値)。"""
    settings = config.load_settings(ROOT)
    universe = config.load_universe(ROOT)
    hist = DATA.history(universe + ["JPY=X"], DAYS)
    fx = float(hist["JPY=X"]["Close"].iloc[-1])
    equity = float(pd.read_csv(ROOT / "state" / "equity.csv")["equity"].iloc[-1])
    rule_set = rules.load_rules(ROOT / "config" / "rules.yaml")
    target, cap = rule_set["sizing"]["target_weight"] * equity, settings["risk"]["max_position_pct"] * equity
    rows = []
    for t in universe:
        if t not in hist:
            continue
        yen = float(hist[t]["Close"].iloc[-1]) * (fx if config.currency(t, settings) == "USD" else 1)
        qty = int(target // yen)
        rows.append({"ticker": t, "yen_per_share": round(yen), "qty_at_target": qty,
                     "weight": round(qty * yen / equity, 3) if qty else None, "within_cap": yen <= cap})
    out = {"equity": round(equity), "target_yen": round(target), "cap_yen": round(cap), "fx": round(fx, 2),
           "buyable_at_target": sum(r["qty_at_target"] > 0 for r in rows), "of": len(rows),
           "one_share_within_cap": sum(r["within_cap"] for r in rows),
           "not_buyable": [r["ticker"] for r in rows if r["qty_at_target"] == 0],
           "avg_weight_of_buyable": round(float(np.mean([r["weight"] for r in rows if r["weight"]])), 3), "rows": rows}
    (OUT / "afford.json").write_text(json.dumps(out, ensure_ascii=False, indent=2) + "\n")
    return out


def d1():
    """6. 押し目買いの d1 を、同じ通しの条件で指数と比べる。"""
    s = setup(D1)
    c = json.loads((OUT / "continuous.json").read_text())
    a, b = pd.Timestamp(c["period"][0]), pd.Timestamp(c["period"][1])
    r = run(s, a, b)
    idx = cli.index_blend_curve(DATA, DAYS, a, b)
    k = r["metrics"]["exposure"]
    out = {"d1_100万": {**stats(r["equity_curve"]["equity"]), "exposure": k, "trades": r["metrics"]["trades"],
                       "by_sleeve": {n: {x: v[x] for x in ("trades", "total_pnl")} for n, v in r["analysis"]["by_sleeve"].items()}},
           "index_at_d1_exposure": {"k": k, **stats(scaled(idx, k))}}
    r2 = run(s, a, b, halt=0.15)
    out["d1_100万_停止15%"] = {**stats(r2["equity_curve"]["equity"]), "halt_days": r2["halt_days"], "trades": r2["metrics"]["trades"]}
    (OUT / "continuous_d1.json").write_text(json.dumps(out, ensure_ascii=False, indent=2) + "\n")
    return out


def costs():
    """5. 売買コスト (片道) を変えたときの v3 の成績。検証の想定は片道 0.1%。"""
    s = setup(ROOT / "config" / "rules.yaml")
    base = list(s["frames"].values())
    first = min(df.index[0] for df in base) + pd.Timedelta(days=300 + 365)
    last = max(df.index[-1] for df in base)
    out = {}
    for bps in (10, 30, 50, 100, 150):
        r = run(s, first, last, slippage_bps=bps)
        out[f"片道{bps / 100:.1f}%"] = {**stats(r["equity_curve"]["equity"]), "trades": r["metrics"]["trades"]}
    (OUT / "costs.json").write_text(json.dumps(out, ensure_ascii=False, indent=2) + "\n")
    return out


if __name__ == "__main__":
    for name in sys.argv[1:] or ["continuous", "bootstrap", "phase", "afford", "costs", "d1"]:
        res = globals()[name]()
        print(name, "done", file=sys.stderr)
