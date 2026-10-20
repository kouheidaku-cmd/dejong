"""口座状態 (現金・保有・未約定注文) の永続化。state/portfolio.json に保存し、git で履歴を残す。"""
from __future__ import annotations

import csv
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path


@dataclass
class Portfolio:
    cash: float
    positions: dict[str, dict] = field(default_factory=dict)  # ticker -> {qty, avg_cost}
    pending: list[dict] = field(default_factory=list)
    peak_equity: float = 0.0
    next_order_id: int = 1

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

    def market_value(self, prices: dict[str, float]) -> float:
        return sum(p["qty"] * prices.get(t, p["avg_cost"]) for t, p in self.positions.items())

    def equity(self, prices: dict[str, float]) -> float:
        return self.cash + self.market_value(prices)


JOURNAL_FIELDS = [
    "date", "id", "ticker", "side", "qty", "type", "limit_price",
    "status", "price", "commission", "realized_pnl", "rationale",
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
