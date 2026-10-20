"""ブローカー実装の切り替え口 (settings.yaml の broker)。

- manual: 注文案を出し、人が楽天証券で発注する (半自動)
- paper: 仮想売買 (翌営業日の寄付きで約定)

API のある証券会社につなぐ場合は submit / settle を持つクラスを追加する。
"""
from ..portfolio import Portfolio
from .manual import ManualBroker
from .paper import PaperBroker

BROKERS = {"manual": ManualBroker, "paper": PaperBroker}


def get_broker(portfolio: Portfolio, settings: dict):
    name = settings.get("broker", "paper")
    if name not in BROKERS:
        raise NotImplementedError(f"broker '{name}' は未実装です (trader/broker/ に追加してください)")
    return BROKERS[name](portfolio, settings)
