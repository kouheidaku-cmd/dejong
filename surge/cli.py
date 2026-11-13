"""python -m surge <command>

  fetch      日足を取り直す
  plan       今の口座と最新の終値から注文案を作る (reports/surge/<日付>/)
  status     保有・順位・目標までの進み具合を表示する
  backtest   過去の日足で仮想売買する (reports/surge/backtest/<name>/)
  robust     設定を少しずつ変えた16通りで走らせ、成績が設定の偶然に頼っていないかを見る
"""
from __future__ import annotations

import argparse
import dataclasses
import json
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import yaml

from . import backtest as bt
from . import data
from .account import load_account
from .plan import expected_sessions, make_plan, render
from .strategy import Params

ROOT = data.ROOT
JST = ZoneInfo("Asia/Tokyo")


def load_config(path: Path | None = None) -> dict:
    cfg = yaml.safe_load((path or Path(__file__).with_name("config.yaml")).read_text(encoding="utf-8"))
    cfg["params"] = Params.from_dict(cfg["params"])
    return cfg


def _pct(x):
    return "-" if x is None else f"{x:+.1%}"


def _summary(name: str, s: dict) -> str:
    h = s.get("h4m", {})
    return (f"{name:16s} 年率 {s['cagr']:+6.1%}  最大下落 {s['max_drawdown']:5.1%}  シャープ {s['sharpe']:.2f} | "
            f"4か月: 中央値 {h.get('median', 0):+5.1%}  +20%以上 {h.get('p_ge_20', 0):4.0%}  マイナス {h.get('p_lt_0', 0):4.0%}  "
            f"-10%以下 {h.get('p_le_m10', 0):4.0%}  悪い方5% {h.get('p05', 0):+6.1%}  最悪 {h.get('worst', 0):+6.1%}")


def cmd_fetch(args, cfg):
    u = data.load_universe()
    names = u["tickers"] + list(u["indices"].values()) + list(u["fx"].values())
    failed = data.fetch(names, force=args.force)
    print(f"取得 {len(names) - len(failed)} / {len(names)}" + (f"  失敗: {failed}" if failed else ""))


def cmd_backtest(args, cfg):
    panel = data.load_panel(refresh=not args.no_fetch)
    p = cfg["params"]
    cost = cfg["execution"]["cost"] if args.cost is None else args.cost
    r = bt.run(panel, p, start=args.start, end=args.end, capital=args.capital, cost=cost, integer=not args.fractional,
               max_weight_mult=cfg["execution"]["max_weight_mult"])
    start, end = r.equity.index[0], r.equity.index[-1]
    s = r.stats()
    bench = {k: bt.stats(e) for k, e in bt.benchmarks(panel, start, end).items()}
    yearly = bt.yearly_returns(r.equity)
    print(f"期間 {start.date()} 〜 {end.date()}  元手 {args.capital:,.0f}円  片道コスト {cost:.2%}  売買 {s['trades']} 往復 "
          f"(勝率 {s['win_rate']:.0%})  株の割合の平均 {s['avg_exposure']:.0%}")
    print(_summary("surge", s))
    labels = {"universe_ew": "母集団を均等保有", "index_5050": "指数 50/50", "sp500_jpy": "S&P500 (円)", "nikkei": "日経平均"}
    for k, st in bench.items():
        print(_summary(labels[k], st))
    print("年ごと: " + "  ".join(f"{y}:{v:+.0%}" for y, v in yearly.items()))
    out = ROOT / "reports" / "surge" / "backtest" / (args.name or f"{date.today().isoformat()}-run")
    out.mkdir(parents=True, exist_ok=True)
    r.equity.rename("equity").round(0).to_csv(out / "equity.csv", header=True)
    r.trades.round(4).to_csv(out / "trades.csv", index=False)
    (out / "result.json").write_text(json.dumps({
        "period": [str(start.date()), str(end.date())], "capital": args.capital, "cost": cost, "integer": not args.fractional,
        "params": p.to_dict(), "surge": s, "benchmarks": bench, "yearly": {int(y): float(v) for y, v in yearly.items()},
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"→ {out.relative_to(ROOT)}/")


def cmd_robust(args, cfg):
    panel = data.load_panel(refresh=not args.no_fetch)
    base = cfg["params"]
    rows = []
    for n in (4, 5, 6, 8):
        for lb in ((63, 126, 252), (126, 252), (63, 126), (21, 63, 126)):
            p = dataclasses.replace(base, top_n=n, hold_rank=4 * n, lookbacks=lb)
            s = bt.run(panel, p, start=args.start, end=args.end, cost=cfg["execution"]["cost"], integer=False).stats()
            h = s["h4m"]
            rows.append({"top_n": n, "lookbacks": "/".join(map(str, lb)), "cagr": s["cagr"], "max_dd": s["max_drawdown"],
                         "sharpe": s["sharpe"], "4m_median": h["median"], "4m_p_ge_20": h["p_ge_20"], "4m_p_lt_0": h["p_lt_0"],
                         "4m_p05": h["p05"], "4m_worst": h["worst"]})
    d = pd.DataFrame(rows)
    print(d.to_string(index=False, formatters={c: "{:+.1%}".format for c in d.columns if c not in ("top_n", "lookbacks", "sharpe")}))
    num = d.drop(columns=["top_n", "lookbacks"])
    print("\n平均  " + "  ".join(f"{c} {v:+.1%}" if c != "sharpe" else f"{c} {v:.2f}" for c, v in num.mean().items()))
    print("最小  " + "  ".join(f"{c} {v:+.1%}" if c != "sharpe" else f"{c} {v:.2f}" for c, v in num.min().items()))
    print("最大  " + "  ".join(f"{c} {v:+.1%}" if c != "sharpe" else f"{c} {v:.2f}" for c, v in num.max().items()))
    if args.name:
        out = ROOT / "reports" / "surge" / "backtest" / args.name
        out.mkdir(parents=True, exist_ok=True)
        d.round(4).to_csv(out / "robust.csv", index=False)
        print(f"→ {(out / 'robust.csv').relative_to(ROOT)}")


def _today(args) -> date:
    return args.date or datetime.now(JST).date()


def cmd_plan(args, cfg):
    today = _today(args)
    panel = data.load_panel(refresh=not args.no_fetch)
    acct = load_account(ROOT)
    weekly = args.weekly or (not args.daily and today.weekday() >= 5)   # 土日に走らせたら週次
    expected = None if args.date else expected_sessions(pd.Timestamp.now(tz="UTC"))
    plan = make_plan(panel, cfg["params"], acct, cfg["execution"], rebalance=weekly, exclude=tuple(args.exclude or ()),
                     today=today, expected=expected)
    text = render(plan)
    out = ROOT / "reports" / "surge" / today.isoformat()
    out.mkdir(parents=True, exist_ok=True)
    (out / "plan.json").write_text(json.dumps(plan, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (out / "plan.md").write_text(text, encoding="utf-8")
    print(text)
    print(f"→ {out.relative_to(ROOT)}/plan.md, plan.json")


def cmd_status(args, cfg):
    today = _today(args)
    panel = data.load_panel(refresh=not args.no_fetch)
    acct = load_account(ROOT)
    plan = make_plan(panel, cfg["params"], acct, cfg["execution"], rebalance=False, today=today)
    g = cfg["goal"]
    start = date.fromisoformat(str(g["start_date"]))
    deadline = (pd.Timestamp(start) + pd.DateOffset(months=g["months"])).date()
    target = g["start_equity"] * (1 + g["target_return"])
    ret = plan["equity"] / g["start_equity"] - 1
    print(f"株 + 現金 {plan['equity']:,.0f}円 (現金 {plan['cash']:,.0f}円)  開始 {start} の {g['start_equity']:,.0f}円から {ret:+.1%}")
    print(f"目標 {target:,.0f}円 ({g['target_return']:+.0%}) まであと {target - plan['equity']:,.0f}円 "
          f"({target / plan['equity'] - 1:+.1%})。期限 {deadline} まで {(deadline - today).days}日")
    print()
    print(render(plan).split("## 保有", 1)[1].split("## 順位", 1)[0].strip())
    journal = ROOT / "reports" / "surge" / "journal.csv"
    rows = pd.read_csv(journal) if journal.exists() else pd.DataFrame(columns=["date", "equity", "cash", "return_since_start"])
    rows = rows[rows["date"] != today.isoformat()]
    rows.loc[len(rows)] = [today.isoformat(), round(plan["equity"]), round(plan["cash"]), round(ret, 4)]
    journal.parent.mkdir(parents=True, exist_ok=True)
    rows.sort_values("date").to_csv(journal, index=False)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python -m surge", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--no-fetch", action="store_true", help="データを取り直さず、キャッシュだけで動かす")
    ap.add_argument("--date", type=date.fromisoformat, help="実行日 (既定: 日本時間の今日)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("fetch")
    s.add_argument("--force", action="store_true")
    s = sub.add_parser("plan")
    s.add_argument("--weekly", action="store_true", help="入れ替えも判定する (土日は自動でこれになる)")
    s.add_argument("--daily", action="store_true", help="高値からの下げだけ判定する")
    s.add_argument("--exclude", nargs="+", metavar="TICKER", help="裁量で見送る銘柄 (次の順位の銘柄が繰り上がる)")
    sub.add_parser("status")
    for name in ("backtest", "robust"):
        s = sub.add_parser(name)
        s.add_argument("--start", default="2005-06-01")
        s.add_argument("--end")
        s.add_argument("--name")
        if name == "backtest":
            s.add_argument("--capital", type=float, default=400_000.0)
            s.add_argument("--cost", type=float, help="片道コスト (既定: config.yaml)")
            s.add_argument("--fractional", action="store_true", help="株数を整数に丸めない")
    args = ap.parse_args(argv)
    cfg = load_config()
    {"fetch": cmd_fetch, "plan": cmd_plan, "status": cmd_status, "backtest": cmd_backtest, "robust": cmd_robust}[args.cmd](args, cfg)
