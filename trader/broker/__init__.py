"""ブローカー実装の切り替え口。

実口座につなぐ場合は PaperBroker と同じ submit / settle を持つクラスを追加し、
settings.yaml の broker で選ぶ (例: Alpaca, kabuステーション API, 立花証券 e支店 API)。
"""
from ..portfolio import Portfolio
from .paper import PaperBroker


def get_broker(portfolio: Portfolio, settings: dict):
    name = settings.get("broker", "paper")
    if name == "paper":
        return PaperBroker(portfolio, settings)
    raise NotImplementedError(f"broker '{name}' は未実装です (trader/broker/ に追加してください)")
