"""ペーパートレード用ブローカー。

注文は「当日終値を見て発注 → 翌営業日の寄付きで約定」とみなす (先読みを防ぐため)。
注文は当日限り: 翌営業日に約定しなかった指値は失効する。
"""
from __future__ import annotations

from datetime import date

import pandas as pd

from ..portfolio import Portfolio


class PaperBroker:
    def __init__(self, portfolio: Portfolio, settings: dict):
        self.pf = portfolio
        ex = settings.get("execution", {})
        self.slippage = ex.get("slippage_bps", 0) / 10_000
        self.commission = ex.get("commission_bps", 0) / 10_000

    def submit(self, order: dict, placed_on: date) -> dict:
        entry = {
            "id": self.pf.next_order_id,
            "placed_on": placed_on.isoformat(),
            "ticker": order["ticker"],
            "side": order["side"],
            "qty": int(order["qty"]),
            "type": order.get("type", "market"),
            "limit_price": order.get("limit_price"),
            "rationale": order.get("rationale", ""),
        }
        self.pf.next_order_id += 1
        self.pf.pending.append(entry)
        return entry

    def settle(self, data: dict[str, pd.DataFrame]) -> list[dict]:
        """未約定注文を、発注日より後の最初の日足で約定させる。まだ日足がなければ持ち越す。"""
        events, still_pending = [], []
        for o in self.pf.pending:
            df = data.get(o["ticker"])
            bars = df[df.index > pd.Timestamp(o["placed_on"])] if df is not None else None
            if bars is None or bars.empty:
                still_pending.append(o)
                continue
            bar = bars.iloc[0]
            events.append(self._fill(o, bar, bars.index[0].date().isoformat()))
        self.pf.pending = still_pending
        return events

    def _fill(self, o: dict, bar: pd.Series, day: str) -> dict:
        ev = {**{k: v for k, v in o.items() if k != "placed_on"}, "date": day,
              "price": None, "commission": 0.0, "realized_pnl": None}
        price = self._price(o, bar)
        if price is None:
            return {**ev, "status": "expired"}

        qty, ticker = o["qty"], o["ticker"]
        gross = price * qty
        fee = gross * self.commission
        if o["side"] == "buy":
            if gross + fee > self.pf.cash:
                return {**ev, "status": "rejected:insufficient_cash"}
            pos = self.pf.positions.setdefault(ticker, {"qty": 0, "avg_cost": 0.0})
            pos["avg_cost"] = (pos["avg_cost"] * pos["qty"] + gross + fee) / (pos["qty"] + qty)
            pos["qty"] += qty
            self.pf.cash -= gross + fee
            pnl = None
        else:
            held = self.pf.qty(ticker)
            if qty > held:
                return {**ev, "status": "rejected:insufficient_shares"}
            pos = self.pf.positions[ticker]
            pnl = round(gross - fee - pos["avg_cost"] * qty, 2)
            pos["qty"] -= qty
            if pos["qty"] == 0:
                del self.pf.positions[ticker]
            self.pf.cash += gross - fee
        return {**ev, "status": "filled", "price": round(price, 4),
                "commission": round(fee, 2), "realized_pnl": pnl}

    def _price(self, o: dict, bar: pd.Series) -> float | None:
        open_, buy = float(bar["Open"]), o["side"] == "buy"
        if o["type"] == "market":
            return open_ * (1 + self.slippage if buy else 1 - self.slippage)
        limit = float(o["limit_price"])
        if buy:
            return min(open_, limit) if float(bar["Low"]) <= limit else None
        return max(open_, limit) if float(bar["High"]) >= limit else None
