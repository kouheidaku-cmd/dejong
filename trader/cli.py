"""コマンドライン。スキルからはこのコマンドだけを使う。

  python -m trader init                 口座を初期化
  python -m trader prep                 日次: 前日注文の約定処理 + 判断材料 (snapshot.json) の作成
  python -m trader submit ORDERS.json   日次: リスクチェックして発注
  python -m trader status               現在の口座状況
  python -m trader screen               週次: 母集団をスクリーニング
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

from . import config, indicators
from .broker import get_broker
from .data import MarketData, YahooData, last_closes
from .portfolio import Portfolio, append_journal, record_equity
from .risk import check_orders


class Paths:
    def __init__(self, root: Path):
        self.root = root
        self.portfolio = root / "state" / "portfolio.json"
        self.journal = root / "state" / "trades.csv"
        self.equity = root / "state" / "equity.csv"
        self.stop = root / "STOP"

    def daily(self, day: date) -> Path:
        return self.root / "reports" / "daily" / day.isoformat()

    def weekly(self, day: date) -> Path:
        y, w, _ = day.isocalendar()
        return self.root / "reports" / "weekly" / f"{y}-W{w:02d}"


def cmd_init(root: Path, cash: float | None, force: bool) -> dict:
    p, settings = Paths(root), config.load_settings(root)
    if p.portfolio.exists() and not force:
        raise SystemExit(f"{p.portfolio} は既にあります (上書きするなら --force)")
    pf = Portfolio(cash=float(cash or settings["initial_cash"]))
    pf.peak_equity = pf.cash
    pf.save(p.portfolio)
    return {"cash": pf.cash}


def cmd_prep(root: Path, data: MarketData, day: date) -> dict:
    p, settings = Paths(root), config.load_settings(root)
    watchlist = config.load_watchlist(root)
    pf = Portfolio.load(p.portfolio)

    tickers = {w["ticker"] for w in watchlist} | set(pf.positions) | {o["ticker"] for o in pf.pending}
    hist = data.history(sorted(tickers), settings["history_days"])

    events = get_broker(pf, settings).settle(hist)
    append_journal(p.journal, events)

    prices = last_closes(hist)
    equity = pf.equity(prices)
    pf.peak_equity = max(pf.peak_equity, equity)
    record_equity(p.equity, day.isoformat(), pf.cash, equity)
    pf.save(p.portfolio)

    summaries = {t: indicators.summarize(df) for t, df in hist.items()}
    latest_bar = max((df.index[-1].date() for df in hist.values()), default=None)
    snapshot = {
        "date": day.isoformat(),
        "latest_bar_date": latest_bar.isoformat() if latest_bar else None,
        "fresh": latest_bar == day,
        "stopped": p.stop.exists(),
        "account": {
            "cash": round(pf.cash, 2),
            "equity": round(equity, 2),
            "peak_equity": round(pf.peak_equity, 2),
            "drawdown": round(1 - equity / pf.peak_equity, 4) if pf.peak_equity else 0.0,
            "cash_pct": round(pf.cash / equity, 4) if equity else None,
        },
        "positions": [
            {
                "ticker": t,
                "qty": pos["qty"],
                "avg_cost": round(pos["avg_cost"], 2),
                "close": prices.get(t),
                "weight": round(pos["qty"] * prices.get(t, pos["avg_cost"]) / equity, 4),
                "unrealized_pct": round(prices[t] / pos["avg_cost"] - 1, 4) if t in prices else None,
                "indicators": summaries.get(t),
            }
            for t, pos in pf.positions.items()
        ],
        "settled_orders": events,
        "pending_orders": pf.pending,
        "watchlist": [
            {**w, "held": pf.qty(w["ticker"]), "lot_size": config.lot_size(w["ticker"], settings),
             "indicators": summaries.get(w["ticker"])}
            for w in watchlist
        ],
        "risk_limits": settings["risk"],
    }
    out = p.daily(day)
    out.mkdir(parents=True, exist_ok=True)
    _write_json(out / "snapshot.json", snapshot)
    return snapshot


def cmd_submit(root: Path, data: MarketData, day: date, orders_path: Path, force: bool) -> dict:
    p, settings = Paths(root), config.load_settings(root)
    result_path = p.daily(day) / "orders_result.json"
    if result_path.exists() and not force:
        raise SystemExit(f"本日は発注済みです ({result_path})。二重発注を防ぐため中止しました")

    orders = json.loads(orders_path.read_text(encoding="utf-8")).get("orders", [])
    pf = Portfolio.load(p.portfolio)
    watchlist = {w["ticker"] for w in config.load_watchlist(root)}
    tickers = {o.get("ticker") for o in orders if o.get("ticker")} | set(pf.positions)
    prices = last_closes(data.history(sorted(tickers), 30))

    broker = get_broker(pf, settings)
    accepted, rejected = [], []
    for order, reason in check_orders(orders, pf, prices, watchlist, settings, p.stop.exists()):
        if reason is None:
            accepted.append(broker.submit(order, day))
        else:
            rejected.append({**order, "reason": reason})
    pf.save(p.portfolio)
    append_journal(p.journal, [{**o, "date": day.isoformat(), "status": f"rejected:{o['reason']}"} for o in rejected])

    result = {"date": day.isoformat(), "accepted": accepted, "rejected": rejected}
    result_path.parent.mkdir(parents=True, exist_ok=True)
    _write_json(result_path, result)
    return result


def cmd_status(root: Path, data: MarketData) -> dict:
    p = Paths(root)
    pf = Portfolio.load(p.portfolio)
    prices = last_closes(data.history(sorted(pf.positions), 30))
    equity = pf.equity(prices)
    return {
        "cash": round(pf.cash, 2),
        "equity": round(equity, 2),
        "positions": {t: {**pos, "close": prices.get(t)} for t, pos in pf.positions.items()},
        "pending": pf.pending,
    }


def cmd_screen(root: Path, data: MarketData, day: date, top: int) -> dict:
    p, settings = Paths(root), config.load_settings(root)
    hist = data.history(config.load_universe(root), settings["history_days"])
    ranked = indicators.screen({t: indicators.summarize(df) for t, df in hist.items()})
    result = {"date": day.isoformat(), "universe_size": len(hist), "ranked": ranked}
    out = p.weekly(day)
    out.mkdir(parents=True, exist_ok=True)
    _write_json(out / "screen.json", result)
    return {**result, "ranked": ranked[:top]}


def _write_json(path: Path, obj) -> None:
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="trader")
    ap.add_argument("--root", type=Path, default=config.ROOT)
    ap.add_argument("--date", type=date.fromisoformat, help="実行日 (既定: settings の timezone での今日)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("init")
    s.add_argument("--cash", type=float)
    s.add_argument("--force", action="store_true")
    sub.add_parser("prep")
    s = sub.add_parser("submit")
    s.add_argument("orders", type=Path)
    s.add_argument("--force", action="store_true")
    sub.add_parser("status")
    s = sub.add_parser("screen")
    s.add_argument("--top", type=int, default=15)
    args = ap.parse_args(argv)

    root = args.root
    day = args.date or config.today(config.load_settings(root))
    data = YahooData()
    if args.cmd == "init":
        out = cmd_init(root, args.cash, args.force)
    elif args.cmd == "prep":
        out = cmd_prep(root, data, day)
    elif args.cmd == "submit":
        out = cmd_submit(root, data, day, args.orders, args.force)
    elif args.cmd == "status":
        out = cmd_status(root, data)
    else:
        out = cmd_screen(root, data, day, args.top)
    json.dump(out, sys.stdout, ensure_ascii=False, indent=2, default=str)
    print()
