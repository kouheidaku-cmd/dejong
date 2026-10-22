"""コマンドライン。スキルからはこのコマンドだけを使う。

  python -m trader init --from FILE     口座を初期化 (FILE は sync と同じ形式。省略時は現金のみ)
  python -m trader prep                 日次: 前回注文の処理 + 判断材料 (snapshot.json) の作成
  python -m trader submit ORDERS.json   日次: リスクチェックして発注 (manual では注文案として記録)
  python -m trader record ...           manual: 人が発注して約定した1件を記録
  python -m trader sync FILE            manual: 証券口座の保有一覧で記録を上書き
  python -m trader status               現在の口座状況
  python -m trader screen               週次: 母集団をスクリーニング
  python -m trader backtest             config/rules.yaml のルールを過去の日足で仮想売買
  python -m trader yearly RULES...      暦年ごとに独立して走らせ、指数 50/50 と比べる
  python -m trader forward              決めた日以降の実データでルールを記録 (フォワードテスト)
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

from . import backtest, config, indicators, rules
from .broker import get_broker
from .data import CachedData, MarketData, YahooData, fx_rates, last_closes
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

    rule_set = rules.load_rules(root / "config" / "rules.yaml")
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
        "rule_signals": rule_signals(pf, hist, prices, fx, equity, watchlist, rule_set, settings, day,
                                     market_regimes(data, hist, rule_set, settings, settings["history_days"])),
        "risk_limits": settings["risk"],
    }
    out = p.daily(day)
    out.mkdir(parents=True, exist_ok=True)
    _write_json(out / "snapshot.json", snapshot)
    return snapshot


def market_regimes(data: MarketData, tickers, rule_set: dict, settings: dict, days: int) -> dict[str, pd.Series]:
    """entry.market_filter を使うとき、銘柄ごとに対応する市場指数の判定を返す。"""
    mf = rules.market_filter(rule_set)
    if not mf:
        return {}
    hist = data.history(sorted(set(mf["indices"].values())), days)
    reg = {c: rules.market_regime(hist[sym], mf["sma"]) for c, sym in mf["indices"].items() if sym in hist}
    return {t: reg[config.currency(t, settings)] for t in tickers if config.currency(t, settings) in reg}


def rule_signals(pf, hist, prices, fx, equity, watchlist, rule_set, settings, day, market=None) -> dict:
    """config/rules.yaml のルールを今日の終値に当てはめた結果。日次の判断はこれを基準にする。"""
    exits = []
    for t, pos in pf.positions.items():
        if t not in hist:
            exits.append({"ticker": t, "signal": None, "note": "価格データなし"})
            continue
        f = rules.features(hist[t])
        opened = pd.Timestamp(pos.get("opened") or f.index[-1])
        ref = pos.get("rule_ref") or pos["avg_cost"]  # 損切り・利確の基準価格 (既存保有は起点日の価格)
        peak = max(f.loc[f.index >= opened, "close"].max(), ref) if (f.index >= opened).any() else ref
        sig = rules.exit_check(f.iloc[-1].to_dict(), ref, peak, (pd.Timestamp(day) - opened).days,
                               pos.get("took_profit", False), rule_set)
        exits.append({
            "ticker": t, "signal": sig[0] if sig else None,
            "sell_qty": rules.sell_qty(pos["qty"], sig[1]) if sig else 0,
            "return_from_cost": round(prices[t] / pos["avg_cost"] - 1, 4),
            "rule_ref": ref,
            "return_from_ref": round(prices[t] / ref - 1, 4),
            "drawdown_from_peak": round(prices[t] / peak - 1, 4),
        })
    sz = rule_set["sizing"]
    slots = sz["max_positions"] - len(pf.positions) + sum(1 for e in exits if e["signal"] and e["sell_qty"] == pf.qty(e["ticker"]))
    entries = []
    for w in watchlist:
        t = w["ticker"]
        if t in pf.positions or t not in hist:
            continue
        f = rules.features(hist[t])
        if market and t in market:
            f = rules.attach_market(f, market[t])
        row = f.iloc[-1].to_dict()
        fails = rules.entry_check(row, rule_set)
        unit = prices[t] * fx[config.currency(t, settings)]
        entries.append({"ticker": t, "ok": not fails, "fails": fails,
                        "rank_value": round(row.get(sz["rank_by"]) or 0, 4),
                        "suggested_qty": int(sz["target_weight"] * equity // unit) if unit else 0})
    entries.sort(key=lambda e: (not e["ok"], -e["rank_value"]))
    market_ok = {config.currency(t, settings): bool(m.iloc[-1]) for t, m in (market or {}).items() if m.notna().iloc[-1]}
    return {"rules_version": rule_set.get("version"), "market_uptrend": market_ok,
            "open_slots": max(slots, 0), "exits": exits, "entries": entries}


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
               price: float, rate: float | None, fee: float, take_profit: bool = False) -> dict:
    """人が発注して約定した注文を記録する。対応する注文案があれば消す。"""
    p, settings = Paths(root), config.load_settings(root)
    pf = Portfolio.load(p.portfolio)
    ccy = config.currency(ticker, settings)
    if rate is None:
        rate = fx_rates(data, [ticker], settings)[ccy]
    pnl = apply_fill(pf, ticker, side, qty, price, ccy, rate, fee, day.isoformat(), take_profit)

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
    old = pf.positions
    pf.positions = {}
    for t, v in (spec.get("positions") or {}).items():
        t = str(t)
        if int(v["qty"]) <= 0:
            continue
        prev = old.get(t, {})
        pf.positions[t] = {
            "qty": int(v["qty"]), "avg_cost": float(v["avg_cost"]), "currency": config.currency(t, settings),
            "opened": prev.get("opened") or str(spec.get("date") or ""),
            "took_profit": bool(prev.get("took_profit", False)),
            "rule_ref": float(v["rule_ref"]) if v.get("rule_ref") else prev.get("rule_ref"),
            "account": v.get("account") or prev.get("account"),  # 口座区分 (nisa_growth / tokutei など)
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


def cmd_backtest(root: Path, data: MarketData, rules_path: Path, years: int, split: date | None,
                 tickers: list[str] | None, name: str) -> dict:
    """ルールを過去データで検証する。期間を学習用 (前半) と検証用 (後半) に分けて別々に走らせる。"""
    rule_set = rules.load_rules(rules_path)
    frames, settings = _load_frames(root, data, tickers or config.load_universe(root), years * 365 + 300)

    first = min(df.index[0] for df in frames.values()) + pd.Timedelta(days=300)  # SMA200 のウォームアップ
    last = max(df.index[-1] for df in frames.values())
    split_ts = pd.Timestamp(split) if split else first + (last - first) * 0.7
    ex = settings.get("execution", {})
    kw = {"slippage_bps": ex.get("slippage_bps", 10), "commission_bps": ex.get("commission_bps", 0)}

    market = market_regimes(data, frames, rule_set, settings, years * 365 + 300)
    out_dir = root / "reports" / "backtest" / name
    out_dir.mkdir(parents=True, exist_ok=True)
    result = {"rules": str(rules_path), "rules_version": rule_set.get("version"), "tickers": len(frames)}
    for label, a, b in [("in_sample", first, split_ts - pd.Timedelta(days=1)), ("out_of_sample", split_ts, last)]:
        r = backtest.run(frames, rule_set, a.date(), b.date(), market=market, **kw)
        pd.DataFrame(r.pop("trades")).to_csv(out_dir / f"trades_{label}.csv", index=False)
        r.pop("equity_curve").to_csv(out_dir / f"equity_{label}.csv")
        result[label] = r
    _write_json(out_dir / "result.json", result)
    for label in ("in_sample", "out_of_sample"):  # 画面には要点だけ出す
        result[label] = {k: result[label][k] for k in ("period", "metrics", "benchmark", "yearly")} | {
            "by_exit_reason": result[label]["analysis"].get("by_exit_reason")}
    result["output"] = str(out_dir)
    return result


def _load_frames(root: Path, data: MarketData, tickers, days: int):
    settings = config.load_settings(root)
    frames = data.history(tickers, days)
    fx_syms = {c: settings["fx_tickers"][c] for c in {config.currency(t, settings) for t in frames} - {settings["base_currency"]}}
    fx_hist = data.history(sorted(fx_syms.values()), days)
    return backtest.to_base_currency(frames, {c: fx_hist[s]["Close"] for c, s in fx_syms.items()}, settings), settings


def index_blend_curve(data: MarketData, days: int, start: pd.Timestamp, end: pd.Timestamp) -> pd.Series:
    """日経平均と S&P500 (円換算) を期間の最初に半分ずつ買って持ち続けた場合 (初期値1)。"""
    h = data.history(["^N225", "^GSPC", "JPY=X"], days)
    spx = h["^GSPC"]["Close"] * h["JPY=X"]["Close"].reindex(h["^GSPC"].index, method="ffill")
    parts = []
    for s in (h["^N225"]["Close"], spx):
        x = s[(s.index >= start) & (s.index <= end)].dropna()
        if not x.empty:  # 期間の初日にまだ日足がない市場は、最初の日足から数える
            parts.append(x / x.iloc[0])
    if not parts:
        return pd.Series([1.0], index=[start])
    return pd.concat(parts, axis=1).sort_index().ffill().fillna(1.0).mean(axis=1)


def cmd_yearly(root: Path, data: MarketData, rules_paths: list[Path], years: int, until: date | None, name: str) -> dict:
    """暦年ごとに独立して (毎年100万円から) 走らせ、指数 50/50 と比べる。複数のルールを同じ条件で比較できる。"""
    days = years * 365 + 300
    frames, settings = _load_frames(root, data, config.load_universe(root), days)
    first = min(df.index[0] for df in frames.values())
    last = max(df.index[-1] for df in frames.values())
    if until:
        last = min(last, pd.Timestamp(until))
    warm = first + pd.Timedelta(days=380)  # SMA200 と12か月モメンタムのウォームアップ
    first_year = warm.year if warm.month == 1 and warm.day <= 15 else warm.year + 1
    ex = settings.get("execution", {})
    kw = {"slippage_bps": ex.get("slippage_bps", 10), "commission_bps": ex.get("commission_bps", 0)}

    windows = [(pd.Timestamp(y, 1, 1), min(pd.Timestamp(y, 12, 31), last)) for y in range(first_year, last.year + 1)]
    out = {"until": last.date().isoformat(), "index_50_50": {}, "rules": {}}
    for a, b in windows:
        c = index_blend_curve(data, days, a, b)
        out["index_50_50"][a.year] = {"return": backtest._r(c.iloc[-1] - 1),
                                      "max_drawdown": backtest._r((1 - c / c.cummax()).max())}
    for rp in rules_paths:
        rule_set = rules.load_rules(rp)
        market = market_regimes(data, frames, rule_set, settings, days)
        per_year = {}
        for a, b in windows:
            r = backtest.run(frames, rule_set, a.date(), b.date(), market=market, **kw)
            m = r["metrics"]
            per_year[a.year] = {"return": m["total_return"], "max_drawdown": m["max_drawdown"], "trades": m["trades"]}
        rets = [v["return"] for v in per_year.values()]
        out["rules"][rp.stem] = {
            "years": per_year,
            "positive_years": sum(r > 0 for r in rets),
            "worst_year": min(rets),
            "chained_return": backtest._r(np.prod([1 + r for r in rets]) - 1),
            "avg_max_drawdown": backtest._r(np.mean([v["max_drawdown"] for v in per_year.values()])),
        }
    idx = list(out["index_50_50"].values())
    out["index_summary"] = {
        "positive_years": sum(v["return"] > 0 for v in idx),
        "worst_year": min(v["return"] for v in idx),
        "chained_return": backtest._r(np.prod([1 + v["return"] for v in idx]) - 1),
        "avg_max_drawdown": backtest._r(np.mean([v["max_drawdown"] for v in idx])),
    }
    out_dir = root / "reports" / "backtest" / name
    out_dir.mkdir(parents=True, exist_ok=True)
    _write_json(out_dir / "yearly.json", out)
    out["output"] = str(out_dir)
    return out


def cmd_forward(root: Path, data: MarketData) -> dict:
    """config/forward.yaml のルールを、start の日から最新のデータまで仮想売買する (毎回最初から計算し直す)。"""
    spec = config.load_yaml(root / "config" / "forward.yaml")
    start = pd.Timestamp(spec["start"])
    days = 450 + (pd.Timestamp.today().normalize() - start).days  # SMA200・12か月モメンタムのウォームアップ込み
    frames, settings = _load_frames(root, data, config.load_universe(root), days)
    last = max(df.index[-1] for df in frames.values())
    ex = settings.get("execution", {})
    kw = {"slippage_bps": ex.get("slippage_bps", 10), "commission_bps": ex.get("commission_bps", 0)}

    out = {"start": start.date().isoformat(), "until": last.date().isoformat(), "rules": {}}
    for item in spec["rules"]:
        rule_set = rules.load_rules(root / item["path"])
        market = market_regimes(data, frames, rule_set, settings, days)
        r = backtest.run(frames, rule_set, start.date(), last.date(), market=market, **kw)
        m = r["metrics"]
        out["rules"][item["name"]] = {
            "label": item.get("label", ""),
            "return": m.get("total_return"), "max_drawdown": m.get("max_drawdown"), "trades": m.get("trades"),
            "open_positions": r["open_positions"],
        }
    c = index_blend_curve(data, days, start, last)
    out["index_50_50"] = {"return": backtest._r(c.iloc[-1] - 1), "max_drawdown": backtest._r((1 - c / c.cummax()).max())}

    out_dir = root / "reports" / "forward"
    out_dir.mkdir(parents=True, exist_ok=True)
    _write_json(out_dir / "latest.json", out)
    hist = out_dir / "history.csv"
    rows = [f"{out['until']},{n},{v['return']},{v['max_drawdown']},{v['trades']}" for n, v in out["rules"].items()]
    rows.append(f"{out['until']},index_50_50,{out['index_50_50']['return']},{out['index_50_50']['max_drawdown']},")
    lines = hist.read_text().splitlines() if hist.exists() else ["date,name,return,max_drawdown,trades"]
    lines = [l for l in lines if not l.startswith(out["until"] + ",")] + rows  # 同じ日の再実行は上書き
    hist.write_text("\n".join(lines) + "\n")
    for v in out["rules"].values():  # 画面には銘柄の一覧を出さない
        v["open_positions"] = [p["ticker"] for p in v["open_positions"]]
    return out


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
    s.add_argument("--take-profit", action="store_true", help="ルールの利確シグナルによる一部売りのとき")
    s = sub.add_parser("sync")
    s.add_argument("source", type=Path)
    sub.add_parser("status")
    s = sub.add_parser("screen")
    s.add_argument("--top", type=int, default=15)
    s = sub.add_parser("backtest")
    s.add_argument("--rules", type=Path, default=None, help="既定: config/rules.yaml")
    s.add_argument("--years", type=int, default=8)
    s.add_argument("--split", type=date.fromisoformat, help="検証期間の開始日 (既定: 後ろ30%%)")
    s.add_argument("--tickers", nargs="+", help="既定: config/universe.yaml")
    s.add_argument("--name", help="出力先 reports/backtest/<name> (既定: 日付-ルール名)")
    sub.add_parser("forward", help="config/forward.yaml のルールを、決めた日以降の実データで記録する")
    s = sub.add_parser("yearly", help="暦年ごとに独立して走らせ、指数 50/50 と比べる")
    s.add_argument("rules", type=Path, nargs="+", help="比較するルールファイル")
    s.add_argument("--years", type=int, default=8)
    s.add_argument("--until", type=date.fromisoformat, help="この日までのデータで評価")
    s.add_argument("--name", required=True, help="出力先 reports/backtest/<name>")
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
        out = cmd_record(root, data, day, args.ticker, args.side, args.qty, args.price, args.fx, args.fee, args.take_profit)
    elif args.cmd == "sync":
        out = cmd_sync(root, day, args.source)
    elif args.cmd == "status":
        out = cmd_status(root, data)
    elif args.cmd == "forward":
        out = cmd_forward(root, CachedData(data, root / "data" / "cache"))
    elif args.cmd == "yearly":
        out = cmd_yearly(root, CachedData(data, root / "data" / "cache"), args.rules, args.years, args.until, args.name)
    elif args.cmd == "screen":
        out = cmd_screen(root, data, day, args.top)
    else:
        rules_path = args.rules or root / "config" / "rules.yaml"
        name = args.name or f"{day.isoformat()}-{rules_path.stem}"
        out = cmd_backtest(root, CachedData(data, root / "data" / "cache"), rules_path, args.years, args.split, args.tickers, name)
    json.dump(out, sys.stdout, ensure_ascii=False, indent=2, default=str)
    print()
