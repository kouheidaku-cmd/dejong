"""ペーパートレード用ブローカー。

注文は「当日終値を見て発注 → 翌営業日の寄付きで約定」とみなす (先読みを防ぐため)。
注文は当日限り: 翌営業日に約定しなかった指値は失効する。売りを先に処理し、売却代金で買える。
"""
from __future__ import annotations

from datetime import date

import pandas as pd

from ..config import currency
from ..portfolio import Portfolio, apply_fill


def new_entry(pf: Portfolio, order: dict, placed_on: date, status: str) -> dict:
    entry = {
        "id": pf.next_order_id,
        "placed_on": placed_on.isoformat(),
        "status": status,
        "ticker": order["ticker"],
        "side": order["side"],
        "qty": int(order["qty"]),
        "type": order.get("type", "market"),
        "limit_price": order.get("limit_price"),
        "rationale": order.get("rationale", ""),
    }
    pf.next_order_id += 1
    pf.pending.append(entry)
    return entry


class PaperBroker:
    def __init__(self, portfolio: Portfolio, settings: dict):
        self.pf = portfolio
        self.settings = settings
        ex = settings.get("execution", {})
        self.slippage = ex.get("slippage_bps", 0) / 10_000
        self.commission = ex.get("commission_bps", 0) / 10_000

    def submit(self, order: dict, placed_on: date) -> dict:
        return new_entry(self.pf, order, placed_on, "pending")

    def settle(self, data: dict[str, pd.DataFrame], fx: dict[str, float], today: date) -> list[dict]:
        """未約定注文を、発注日より後の最初の日足で約定させる。まだ日足がなければ持ち越す。"""
        events, still_pending = [], []
        for o in sorted(self.pf.pending, key=lambda o: o["side"] != "sell"):
            df = data.get(o["ticker"])
            bars = df[df.index > pd.Timestamp(o["placed_on"])] if df is not None else None
            if bars is None or bars.empty:
                still_pending.append(o)
                continue
            events.append(self._fill(o, bars.iloc[0], bars.index[0].date().isoformat(), fx))
        self.pf.pending = still_pending
        return events

    def _fill(self, o: dict, bar: pd.Series, day: str, fx: dict[str, float]) -> dict:
        ccy = currency(o["ticker"], self.settings)
        ev = {**{k: v for k, v in o.items() if k != "placed_on"}, "date": day, "currency": ccy}
        price = self._price(o, bar)
        if price is None:
            return {**ev, "status": "expired"}
        rate = fx[ccy]
        fee = price * o["qty"] * rate * self.commission
        if o["side"] == "buy" and price * o["qty"] * rate + fee > self.pf.cash:
            return {**ev, "status": "rejected:insufficient_cash"}
        try:
            pnl = apply_fill(self.pf, o["ticker"], o["side"], o["qty"], price, ccy, rate, fee)
        except ValueError:
            return {**ev, "status": "rejected:insufficient_shares"}
        return {**ev, "status": "filled", "price": round(price, 4), "fx": rate,
                "commission": round(fee, 2), "realized_pnl": pnl}

    def _price(self, o: dict, bar: pd.Series) -> float | None:
        open_, buy = float(bar["Open"]), o["side"] == "buy"
        if o["type"] == "market":
            return open_ * (1 + self.slippage if buy else 1 - self.slippage)
        limit = float(o["limit_price"])
        if buy:
            return min(open_, limit) if float(bar["Low"]) <= limit else None
        return max(open_, limit) if float(bar["High"]) >= limit else None
