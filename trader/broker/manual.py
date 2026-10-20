"""半自動ブローカー: Claude は注文案を出すだけで、人が楽天証券のアプリで発注する。

- submit: リスクチェックを通った注文を「注文案 (proposed)」として記録する
- 約定の反映は人の報告を元に `trader record` (1件ずつ) か `trader sync` (保有一覧で上書き) で行う
- 翌日以降まで報告がなかった注文案は失効扱いにし、snapshot で確認を促す
"""
from __future__ import annotations

from datetime import date

import pandas as pd

from ..portfolio import Portfolio
from .paper import new_entry


class ManualBroker:
    def __init__(self, portfolio: Portfolio, settings: dict):
        self.pf = portfolio

    def submit(self, order: dict, placed_on: date) -> dict:
        return new_entry(self.pf, order, placed_on, "proposed")

    def settle(self, data: dict[str, pd.DataFrame], fx: dict[str, float], today: date) -> list[dict]:
        expired = [o for o in self.pf.pending if o["placed_on"] < today.isoformat()]
        self.pf.pending = [o for o in self.pf.pending if o not in expired]
        return [{**o, "date": today.isoformat(), "status": "proposal_unconfirmed"} for o in expired]
