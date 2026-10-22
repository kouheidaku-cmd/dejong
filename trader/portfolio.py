"""口座状態 (現金・保有・未約定注文) の永続化。state/portfolio.json に保存し、git で履歴を残す。

金額の単位:
- cash, peak_equity, other_assets は基準通貨 (円)。楽天の円貨決済を想定し、外貨預り金も円換算して合算する
- positions の avg_cost は銘柄の現地通貨 (楽天の画面表示と同じ)
"""
from __future__ import annotations

import csv
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path


@dataclass
class Portfolio:
    cash: float
    # ticker -> {qty, avg_cost, currency, opened (保有開始日), took_profit (一部利確済みか),
    #            rule_ref (損切り・利確の基準価格。なければ avg_cost), account (口座区分)}
    positions: dict[str, dict] = field(default_factory=dict)
    pending: list[dict] = field(default_factory=list)
    peak_equity: float = 0.0
    next_order_id: int = 1
    other_assets: float = 0.0  # 投資信託など売買対象外の資産 (表示用)

    @classmethod
    def load(cls, path: Path) -> "Portfolio":
        with open(path, encoding="utf-8") as f:
            return cls(**json.load(f))

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(asdict(self), f, ensure_ascii=False, indent=2)
            f.write("\n")

    def qty(self, ticker: str) -> int:
        return int(self.positions.get(ticker, {}).get("qty", 0))

    def value(self, ticker: str, prices: dict[str, float], fx: dict[str, float]) -> float:
        p = self.positions[ticker]
        return p["qty"] * prices.get(ticker, p["avg_cost"]) * fx[p["currency"]]

    def market_value(self, prices: dict[str, float], fx: dict[str, float]) -> float:
        return sum(self.value(t, prices, fx) for t in self.positions)

    def equity(self, prices: dict[str, float], fx: dict[str, float]) -> float:
        return self.cash + self.market_value(prices, fx)


def apply_fill(pf: Portfolio, ticker: str, side: str, qty: int, price: float,
               ccy: str, rate: float, fee: float = 0.0, day: str | None = None,
               take_profit: bool = False) -> float | None:
    """約定を口座に反映し、売りなら実現損益 (円) を返す。

    実現損益は取得単価を約定時のレートで円換算した概算 (為替差損益は区別しない)。
    """
    gross = price * qty * rate
    if side == "buy":
        pos = pf.positions.setdefault(
            ticker, {"qty": 0, "avg_cost": 0.0, "currency": ccy, "opened": day, "took_profit": False})
        pos["avg_cost"] = (pos["avg_cost"] * pos["qty"] + price * qty) / (pos["qty"] + qty)
        pos["qty"] += qty
        pf.cash -= gross + fee
        return None
    held = pf.qty(ticker)
    if qty > held:
        raise ValueError(f"{ticker} の保有 {held} を超える売り {qty}")
    pos = pf.positions[ticker]
    pnl = (price - pos["avg_cost"]) * qty * rate - fee
    pos["qty"] -= qty
    if pos["qty"] == 0:
        del pf.positions[ticker]
    elif take_profit:
        pos["took_profit"] = True  # ルールの利確 (一部売却) は1回だけ
    pf.cash += gross - fee
    return round(pnl, 2)


JOURNAL_FIELDS = [
    "date", "id", "ticker", "side", "qty", "type", "limit_price", "status",
    "price", "currency", "fx", "commission", "realized_pnl", "rationale",
]


def append_journal(path: Path, events: list[dict]) -> None:
    if not events:
        return
    new = not path.exists()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=JOURNAL_FIELDS, extrasaction="ignore")
        if new:
            w.writeheader()
        w.writerows(events)


def record_equity(path: Path, day: str, cash: float, equity: float) -> None:
    """日次の資産推移。同じ日に再実行した場合は上書きする。"""
    rows = []
    if path.exists():
        with open(path, encoding="utf-8") as f:
            rows = [r for r in csv.DictReader(f) if r["date"] != day]
    rows.append({"date": day, "cash": round(cash, 2), "equity": round(equity, 2)})
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["date", "cash", "equity"])
        w.writeheader()
        w.writerows(rows)
