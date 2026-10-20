"""コマンドライン。スキルからはこのコマンドだけを使う。

  python -m trader init --from FILE     口座を初期化 (FILE は sync と同じ形式。省略時は現金のみ)
  python -m trader prep                 日次: 前回注文の処理 + 判断材料 (snapshot.json) の作成
  python -m trader submit ORDERS.json   日次: リスクチェックして発注 (manual では注文案として記録)
  python -m trader record ...           manual: 人が発注して約定した1件を記録
  python -m trader sync FILE            manual: 証券口座の保有一覧で記録を上書き
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
from .data import MarketData, YahooData, fx_rates, last_closes
from .portfolio import Portfolio, append_journal, apply_fill, record_equity
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


def cmd_init(root: Path, cash: float | None, source: Path | None, force: bool) -> dict:
    p, settings = Paths(root), config.load_settings(root)
    if p.portfolio.exists() and not force:
        raise SystemExit(f"{p.portfolio} は既にあります (上書きするなら --force)")
    pf = Portfolio(cash=float(cash or 0))
    if source:
        _apply_sync(pf, config.load_yaml(source), settings)
    pf.save(p.portfolio)
    return {"cash": pf.cash, "positions": pf.positions}


def cmd_prep(root: Path, data: MarketData, day: date) -> dict:
    p, settings = Paths(root), config.load_settings(root)
    watchlist = config.load_watchlist(root)
    pf = Portfolio.load(p.portfolio)

    tickers = {w["ticker"] for w in watchlist} | set(pf.positions) | {o["ticker"] for o in pf.pending}
    hist = data.history(sorted(tickers), settings["history_days"])
    fx = fx_rates(data, tickers, settings)

    events = get_broker(pf, settings).settle(hist, fx, day)
    append_journal(p.journal, events)

    prices = last_closes(hist)
    equity = pf.equity(prices, fx)
    pf.peak_equity = max(pf.peak_equity, equity)
    record_equity(p.equity, day.isoformat(), pf.cash, equity)
    pf.save(p.portfolio)

    summaries = {t: indicators.summarize(df) for t, df in hist.items()}
    latest_bars: dict[str, str] = {}
    for t, df in hist.items():
        ccy = config.currency(t, settings)
        latest_bars[ccy] = max(latest_bars.get(ccy, ""), df.index[-1].date().isoformat())

    snapshot = {
        "date": day.isoformat(),
        "broker": settings.get("broker"),
        "latest_bar_date": latest_bars,  # 市場 (通貨) ごとの最新日足の日付
        "fx": fx,
        "stopped": p.stop.exists(),
        "account": {
            "cash": round(pf.cash),
            "equity": round(equity),
            "other_assets": round(pf.other_assets),
            "total_assets": round(equity + pf.other_assets),
            "peak_equity": round(pf.peak_equity),
            "drawdown": round(1 - equity / pf.peak_equity, 4) if pf.peak_equity else 0.0,
            "cash_pct": round(pf.cash / equity, 4) if equity else None,
        },
        "positions": [
            {
                "ticker": t,
                "qty": pos["qty"],
                "currency": pos["currency"],
                "avg_cost": round(pos["avg_cost"], 2),
                "close": prices.get(t),
                "value_base": round(pf.value(t, prices, fx)),
                "weight": round(pf.value(t, prices, fx) / equity, 4) if equity else None,
                "unrealized_pct": round(prices[t] / pos["avg_cost"] - 1, 4) if t in prices else None,
                "indicators": summaries.get(t),
            }
            for t, pos in pf.positions.items()
        ],
        "settled_orders": events,
        "pending_orders": pf.pending,
        "watchlist": [
            {**w, "held": pf.qty(w["ticker"]), "lot_size": config.lot_size(w["ticker"], settings),
             "currency": config.currency(w["ticker"], settings), "indicators": summaries.get(w["ticker"])}
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
    tickers = {o.get("ticker") for o in orders if o.get("ticker")} | set(pf.positions) | {o["ticker"] for o in pf.pending}
    prices = last_closes(data.history(sorted(tickers), 30))
    fx = fx_rates(data, tickers, settings)

    broker = get_broker(pf, settings)
    accepted, rejected = [], []
    for order, reason in check_orders(orders, pf, prices, fx, watchlist, settings, p.stop.exists()):
        if reason is None:
            accepted.append(broker.submit(order, day))
        else:
            rejected.append({**order, "reason": reason})
    pf.save(p.portfolio)
    append_journal(p.journal, [{**o, "date": day.isoformat(), "status": f"rejected:{o['reason']}"} for o in rejected])

    result = {"date": day.isoformat(), "broker": settings.get("broker"), "accepted": accepted, "rejected": rejected}
    result_path.parent.mkdir(parents=True, exist_ok=True)
    _write_json(result_path, result)
    return result


def cmd_record(root: Path, data: MarketData, day: date, ticker: str, side: str, qty: int,
               price: float, rate: float | None, fee: float) -> dict:
    """人が発注して約定した注文を記録する。対応する注文案があれば消す。"""
    p, settings = Paths(root), config.load_settings(root)
    pf = Portfolio.load(p.portfolio)
    ccy = config.currency(ticker, settings)
    if rate is None:
        rate = fx_rates(data, [ticker], settings)[ccy]
    pnl = apply_fill(pf, ticker, side, qty, price, ccy, rate, fee)

    match = next((o for o in pf.pending if o["ticker"] == ticker and o["side"] == side), None)
    if match:
        pf.pending.remove(match)
    event = {**(match or {}), "date": day.isoformat(), "ticker": ticker, "side": side, "qty": qty,
             "status": "filled", "price": price, "currency": ccy, "fx": rate, "commission": fee,
             "realized_pnl": pnl}
    pf.save(p.portfolio)
    append_journal(p.journal, [event])
    return {"event": event, "cash": round(pf.cash), "warning": "現金がマイナスです。sync で実際の残高に合わせてください" if pf.cash < 0 else None}


def cmd_sync(root: Path, day: date, source: Path) -> dict:
    """証券口座の保有一覧 (YAML) で記録を上書きする。差分は trades.csv に status=sync で残す。"""
    p, settings = Paths(root), config.load_settings(root)
    pf = Portfolio.load(p.portfolio)
    spec = config.load_yaml(source)
    before = {t: pos["qty"] for t, pos in pf.positions.items()}
    cash_before = pf.cash
    _apply_sync(pf, spec, settings)

    diffs = []
    for t in sorted(set(before) | set(pf.positions)):
        d = pf.qty(t) - before.get(t, 0)
        if d:
            diffs.append({"date": day.isoformat(), "ticker": t, "side": "buy" if d > 0 else "sell",
                          "qty": abs(d), "status": "sync", "rationale": f"口座との照合 ({source.name})"})
    if spec.get("reset_peak"):
        pf.peak_equity = 0.0  # 次の prep で現在の総資産になる (入出金があったとき用)
    pf.save(p.portfolio)
    append_journal(p.journal, diffs)
    return {"cash_before": round(cash_before), "cash_after": round(pf.cash), "position_changes": diffs}


def _apply_sync(pf: Portfolio, spec: dict, settings: dict) -> None:
    pf.cash = float(spec["cash_jpy"])
    pf.other_assets = float(spec.get("other_assets_jpy", 0))
    pf.positions = {
        str(t): {"qty": int(v["qty"]), "avg_cost": float(v["avg_cost"]), "currency": config.currency(str(t), settings)}
        for t, v in (spec.get("positions") or {}).items()
        if int(v["qty"]) > 0
    }


def cmd_status(root: Path, data: MarketData) -> dict:
    p, settings = Paths(root), config.load_settings(root)
    pf = Portfolio.load(p.portfolio)
    prices = last_closes(data.history(sorted(pf.positions), 30))
    fx = fx_rates(data, pf.positions, settings)
    return {
        "cash": round(pf.cash),
        "equity": round(pf.equity(prices, fx)),
        "fx": fx,
        "positions": {t: {**pos, "close": prices.get(t), "value_base": round(pf.value(t, prices, fx))}
                      for t, pos in pf.positions.items()},
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
    s.add_argument("--from", dest="source", type=Path)
    s.add_argument("--force", action="store_true")
    sub.add_parser("prep")
    s = sub.add_parser("submit")
    s.add_argument("orders", type=Path)
    s.add_argument("--force", action="store_true")
    s = sub.add_parser("record")
    s.add_argument("ticker")
    s.add_argument("side", choices=["buy", "sell"])
    s.add_argument("qty", type=int)
    s.add_argument("price", type=float, help="約定単価 (現地通貨)")
    s.add_argument("--fx", type=float, help="約定時の為替レート (省略時は最新レート)")
    s.add_argument("--fee", type=float, default=0.0, help="手数料 (円)")
    s = sub.add_parser("sync")
    s.add_argument("source", type=Path)
    sub.add_parser("status")
    s = sub.add_parser("screen")
    s.add_argument("--top", type=int, default=15)
    args = ap.parse_args(argv)

    root = args.root
    day = args.date or config.today(config.load_settings(root))
    data = YahooData()
    if args.cmd == "init":
        out = cmd_init(root, args.cash, args.source, args.force)
    elif args.cmd == "prep":
        out = cmd_prep(root, data, day)
    elif args.cmd == "submit":
        out = cmd_submit(root, data, day, args.orders, args.force)
    elif args.cmd == "record":
        out = cmd_record(root, data, day, args.ticker, args.side, args.qty, args.price, args.fx, args.fee)
    elif args.cmd == "sync":
        out = cmd_sync(root, day, args.source)
    elif args.cmd == "status":
        out = cmd_status(root, data)
    else:
        out = cmd_screen(root, data, day, args.top)
    json.dump(out, sys.stdout, ensure_ascii=False, indent=2, default=str)
    print()
