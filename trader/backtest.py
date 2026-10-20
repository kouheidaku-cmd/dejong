"""過去の日足で config/rules.yaml のルールを仮想売買する。

- 終値でシグナル判定 → 翌営業日の寄付きで約定 (日次運用と同じ)
- 価格はすべて基準通貨 (円) に換算済みのものを受け取る
- 1株単位で売買し、手数料とスリッページを差し引く
- 母集団が「今の大型株」なので、過去の成績は生き残りバイアスで良く見えやすい点に注意
"""
from __future__ import annotations

import math
from datetime import date

import numpy as np
import pandas as pd

from .config import currency
from .rules import entry_check, exit_check, features, sell_qty


def to_base_currency(frames: dict[str, pd.DataFrame], fx: dict[str, pd.Series], settings: dict) -> dict[str, pd.DataFrame]:
    """外貨建ての日足を、その日の為替レートで円に換算する。"""
    out = {}
    for t, df in frames.items():
        ccy = currency(t, settings)
        if ccy == settings["base_currency"]:
            out[t] = df
            continue
        rate = fx[ccy].reindex(df.index).ffill().bfill()
        conv = df.copy()
        for col in ["Open", "High", "Low", "Close"]:
            conv[col] = df[col] * rate
        out[t] = conv
    return out


def run(frames: dict[str, pd.DataFrame], rules: dict, start: date, end: date,
        initial_cash: float = 1_000_000, slippage_bps: float = 10, commission_bps: float = 0) -> dict:
    slip, comm = slippage_bps / 10_000, commission_bps / 10_000
    feats = {t: features(df).to_dict("index") for t, df in frames.items()}
    start_ts, end_ts = pd.Timestamp(start), pd.Timestamp(end)
    dates = sorted({d for f in feats.values() for d in f if start_ts <= d <= end_ts})
    sz = rules["sizing"]

    cash = initial_cash
    pos: dict[str, dict] = {}
    orders: list[dict] = []
    trades, curve, last_close = [], [], {}

    for d in dates:
        # 1. 前日のシグナルを寄付きで約定 (売りが先)
        waiting = []
        for o in sorted(orders, key=lambda o: o["side"] != "sell"):
            row = feats[o["ticker"]].get(d)
            if row is None:
                waiting.append(o)
                continue
            if o["side"] == "sell":
                p = pos[o["ticker"]]
                qty = min(o["qty"], p["qty"])
                price = row["open"] * (1 - slip)
                proceeds = price * qty * (1 - comm)
                cash += proceeds
                trades.append(_trade(o["ticker"], p, qty, price, proceeds, d, o["reason"]))
                p["qty"] -= qty
                if o["reason"] == "take_profit":
                    p["took_profit"] = True
                if p["qty"] == 0:
                    del pos[o["ticker"]]
            else:
                price = row["open"] * (1 + slip)
                qty = min(o["qty"], int(cash // (price * (1 + comm))))
                if qty <= 0:
                    continue
                cash -= price * qty * (1 + comm)
                pos[o["ticker"]] = {"qty": qty, "avg_cost": price * (1 + comm), "peak": row["open"],
                                    "entry_date": d, "took_profit": False, "entry": o["entry"]}
        orders = waiting

        # 2. 終値で評価
        for t, f in feats.items():
            if d in f and not math.isnan(f[d]["close"]):
                last_close[t] = f[d]["close"]
        invested = sum(p["qty"] * last_close.get(t, p["avg_cost"]) for t, p in pos.items())
        curve.append({"date": d, "equity": cash + invested, "invested": invested})
        equity = cash + invested

        # 3. 終値でシグナル判定 → 翌営業日の注文
        selling = {o["ticker"] for o in orders if o["side"] == "sell"}
        full_exits = 0
        for t, p in pos.items():
            row = feats[t].get(d)
            if row is None or t in selling:
                continue
            p["peak"] = max(p["peak"], row["close"])
            sig = exit_check(row, p["avg_cost"], p["peak"], (d - p["entry_date"]).days, p["took_profit"], rules)
            if sig:
                qty = sell_qty(p["qty"], sig[1])
                orders.append({"ticker": t, "side": "sell", "qty": qty, "reason": sig[0]})
                full_exits += qty == p["qty"]

        buying = {o["ticker"] for o in orders if o["side"] == "buy"}
        slots = sz["max_positions"] - (len(pos) - full_exits) - len(buying)
        if slots <= 0:
            continue
        candidates = []
        for t, f in feats.items():
            row = f.get(d)
            if t in pos or t in buying or row is None or entry_check(row, rules):
                continue
            candidates.append((row.get(sz.get("rank_by", "ret_60d")) or 0, t, row))
        for _, t, row in sorted(candidates, reverse=True)[:slots]:
            qty = int(sz["target_weight"] * equity // row["close"])
            if qty > 0:
                orders.append({"ticker": t, "side": "buy", "qty": qty,
                               "entry": {k: _r(row[k]) for k in ("close", "rsi14", "ret_60d", "sma20", "sma50")}})

    eq = pd.DataFrame(curve).set_index("date") if curve else pd.DataFrame(columns=["equity", "invested"])
    open_positions = [
        {"ticker": t, "qty": p["qty"], "avg_cost": _r(p["avg_cost"]), "close": _r(last_close.get(t)),
         "unrealized_pct": _r(last_close.get(t, p["avg_cost"]) / p["avg_cost"] - 1)}
        for t, p in pos.items()
    ]
    return {
        "period": {"start": start.isoformat(), "end": end.isoformat(), "days": len(dates)},
        "metrics": metrics(eq, trades, initial_cash),
        "benchmark": benchmark(frames, start_ts, end_ts),
        "analysis": analyze(trades),
        "open_positions": open_positions,
        "trades": trades,
        "equity_curve": eq,
    }


def _trade(ticker, p, qty, price, proceeds, d, reason) -> dict:
    cost = p["avg_cost"] * qty
    return {
        "ticker": ticker, "entry_date": p["entry_date"].date().isoformat(), "exit_date": d.date().isoformat(),
        "qty": qty, "entry_price": _r(p["avg_cost"]), "exit_price": _r(price),
        "pnl": _r(proceeds - cost), "pnl_pct": _r(proceeds / cost - 1),
        "holding_days": (d - p["entry_date"]).days, "exit_reason": reason,
        **{f"entry_{k}": v for k, v in p["entry"].items()},
    }


def metrics(eq: pd.DataFrame, trades: list[dict], initial_cash: float) -> dict:
    if eq.empty:
        return {}
    e = eq["equity"]
    rets = e.pct_change().dropna()
    years = max((e.index[-1] - e.index[0]).days / 365.25, 1e-9)
    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    gross_loss = -sum(t["pnl"] for t in losses)
    return {
        "final_equity": _r(e.iloc[-1]),
        "total_return": _r(e.iloc[-1] / initial_cash - 1),
        "cagr": _r((e.iloc[-1] / initial_cash) ** (1 / years) - 1),
        "max_drawdown": _r((1 - e / e.cummax()).max()),
        "sharpe": _r(rets.mean() / rets.std() * np.sqrt(252)) if rets.std() > 0 else None,
        "exposure": _r((eq["invested"] / e).mean()),
        "trades": len(trades),
        "win_rate": _r(len(wins) / len(trades)) if trades else None,
        "avg_win_pct": _r(np.mean([t["pnl_pct"] for t in wins])) if wins else None,
        "avg_loss_pct": _r(np.mean([t["pnl_pct"] for t in losses])) if losses else None,
        "profit_factor": _r(sum(t["pnl"] for t in wins) / gross_loss) if gross_loss > 0 else None,
        "avg_holding_days": _r(np.mean([t["holding_days"] for t in trades])) if trades else None,
    }


def benchmark(frames: dict[str, pd.DataFrame], start: pd.Timestamp, end: pd.Timestamp) -> dict:
    """母集団を期間の最初に均等に買って持ち続けた場合。"""
    curves = []
    for df in frames.values():
        c = df["Close"][(df.index >= start) & (df.index <= end)].dropna()
        if len(c) > 1:
            curves.append(c / c.iloc[0])
    if not curves:
        return {}
    bh = pd.concat(curves, axis=1).sort_index().ffill().mean(axis=1)
    return {"total_return": _r(bh.iloc[-1] - 1), "max_drawdown": _r((1 - bh / bh.cummax()).max())}


def analyze(trades: list[dict]) -> dict:
    """損失の分析: 売りの理由別の成績、負けトレードの共通点、ワースト10。"""
    if not trades:
        return {}
    df = pd.DataFrame(trades)
    by_reason = {
        reason: {"trades": len(g), "win_rate": _r((g["pnl"] > 0).mean()), "total_pnl": _r(g["pnl"].sum()),
                 "avg_pnl_pct": _r(g["pnl_pct"].mean()), "avg_holding_days": _r(g["holding_days"].mean())}
        for reason, g in df.groupby("exit_reason")
    }
    by_ticker = df.groupby("ticker")["pnl"].sum().sort_values()
    losers, winners = df[df["pnl"] <= 0], df[df["pnl"] > 0]
    entry_cols = [c for c in df.columns if c.startswith("entry_") and c not in ("entry_date", "entry_price", "entry_close", "entry_sma20", "entry_sma50")]
    compare = {
        c: {"losers": _r(losers[c].mean()) if len(losers) else None, "winners": _r(winners[c].mean()) if len(winners) else None}
        for c in entry_cols
    }
    df["entry_gap_sma20"] = df["entry_close"] / df["entry_sma20"] - 1
    compare["entry_gap_sma20"] = {"losers": _r(df.loc[df["pnl"] <= 0, "entry_gap_sma20"].mean()),
                                  "winners": _r(df.loc[df["pnl"] > 0, "entry_gap_sma20"].mean())}
    worst = df.nsmallest(10, "pnl")
    return {
        "by_exit_reason": by_reason,
        "entry_features_losers_vs_winners": compare,
        "worst_tickers": {t: _r(v) for t, v in by_ticker.head(5).items()},
        "best_tickers": {t: _r(v) for t, v in by_ticker.tail(5)[::-1].items()},
        "worst_trades": worst.drop(columns=["entry_sma20", "entry_sma50"], errors="ignore").to_dict("records"),
    }


def _r(v, n: int = 4):
    if v is None:
        return None
    v = float(v)
    return None if math.isnan(v) or math.isinf(v) else round(v, n)
