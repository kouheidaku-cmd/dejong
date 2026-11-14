"""口座の記録を読む。

記録そのもの (state/portfolio.json, state/trades.csv) は既存の仕組みと共有する: 約定の反映は
`python -m trader record` や sync-account スキルで行い、surge は読むだけで書き換えない。
口座はひとつなので、記録もひとつにしておく。
"""
from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from pathlib import Path

import yaml

from .data import ROOT


@dataclass
class Account:
    cash: float                    # 円
    positions: dict[str, dict]     # ticker -> {qty, avg_cost, currency, opened, ...}
    other_assets: float            # 投資信託など (売買の対象外)
    max_position_pct: float        # settings.yaml の絶対の上限
    min_cash_pct: float
    nisa_remaining: float | None   # NISA 成長投資枠の残り (概算)
    stopped: bool                  # STOP ファイルがある


def load_account(root: Path = ROOT) -> Account:
    pf = json.loads((root / "state" / "portfolio.json").read_text(encoding="utf-8"))
    settings = yaml.safe_load((root / "config" / "settings.yaml").read_text(encoding="utf-8"))
    risk = settings.get("risk", {})
    return Account(
        cash=float(pf["cash"]),
        positions=pf.get("positions", {}),
        other_assets=float(pf.get("other_assets", 0.0)),
        max_position_pct=float(risk.get("max_position_pct", 0.25)),
        min_cash_pct=float(risk.get("min_cash_pct", 0.05)),
        nisa_remaining=_nisa_remaining(root, settings.get("nisa")),
        stopped=(root / "STOP").exists(),
    )


def _nisa_remaining(root: Path, nisa: dict | None) -> float | None:
    """settings.yaml の残り枠 (as_of 時点) から、それより後の約定済みの買いを引いた概算。年が変わっていれば None。"""
    if not nisa or nisa.get("growth_remaining") is None:
        return None
    as_of = str(nisa.get("as_of", ""))
    left = float(nisa["growth_remaining"])
    path = root / "state" / "trades.csv"
    if not path.exists():
        return left
    with open(path, encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            if row["status"] != "filled" or row["side"] != "buy" or row["date"] <= as_of:
                continue
            if row["date"][:4] != str(nisa.get("year")):
                return None
            left -= float(row["qty"]) * float(row["price"]) * float(row["fx"] or 1.0)
    return left
