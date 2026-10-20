import json
import shutil
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from trader import cli, config, indicators
from trader.broker.paper import PaperBroker
from trader.portfolio import Portfolio
from trader.risk import check_orders

ROOT = Path(__file__).resolve().parent.parent
SETTINGS = config.load_settings(ROOT)


def bars(closes, start="2026-01-05"):
    idx = pd.bdate_range(start, periods=len(closes))
    c = pd.Series(closes, index=idx, dtype=float)
    return pd.DataFrame({"Open": c, "High": c * 1.01, "Low": c * 0.99, "Close": c, "Volume": 1000.0})


class FakeData:
    def __init__(self, frames):
        self.frames = frames

    def history(self, tickers, days):
        return {t: self.frames[t] for t in tickers if t in self.frames}


@pytest.fixture
def root(tmp_path):
    shutil.copytree(ROOT / "config", tmp_path / "config")
    cli.cmd_init(tmp_path, 1_000_000, force=False)
    return tmp_path


def test_indicators_on_uptrend():
    s = indicators.summarize(bars(np.linspace(100, 200, 250)))
    assert s["ret_60d"] > 0 and s["rsi14"] > 70
    assert indicators.trend_ok(s)
    ranked = indicators.screen({"UP": s, "DOWN": indicators.summarize(bars(np.linspace(200, 100, 250)))})
    assert [r["ticker"] for r in ranked] == ["UP", "DOWN"]


def test_paper_fill_next_open_and_sell_pnl():
    pf = Portfolio(cash=100_000)
    settings = {"execution": {"slippage_bps": 0, "commission_bps": 0}}
    b = PaperBroker(pf, settings)
    df = bars([100, 110, 120], start="2026-03-02")
    b.submit({"ticker": "X", "side": "buy", "qty": 100}, date(2026, 3, 2))
    assert b.settle({"X": df.iloc[:1]}) == []  # 翌日の足がまだない → 持ち越し
    [ev] = b.settle({"X": df})
    assert ev["status"] == "filled" and ev["price"] == 110 and ev["date"] == "2026-03-03"
    assert pf.cash == 100_000 - 11_000 and pf.qty("X") == 100

    b.submit({"ticker": "X", "side": "sell", "qty": 100}, date(2026, 3, 3))
    [ev] = b.settle({"X": df})
    assert ev["realized_pnl"] == 1_000 and "X" not in pf.positions


def test_limit_order_expires_when_not_touched():
    pf = Portfolio(cash=100_000)
    b = PaperBroker(pf, {})
    b.submit({"ticker": "X", "side": "buy", "qty": 1, "type": "limit", "limit_price": 50}, date(2026, 3, 2))
    [ev] = b.settle({"X": bars([100, 100], start="2026-03-02")})
    assert ev["status"] == "expired" and pf.cash == 100_000 and not pf.pending


def test_risk_rules():
    pf = Portfolio(cash=1_000_000, positions={"7203.T": {"qty": 100, "avg_cost": 2000}}, peak_equity=1_200_000)
    prices = {"7203.T": 2000.0, "6758.T": 1000.0, "9999.T": 500.0}
    wl = {"7203.T", "6758.T"}
    o = lambda **kw: {"side": "buy", "type": "market", "rationale": "test", **kw}
    results = check_orders(
        [
            o(ticker="6758.T", qty=100),                 # OK
            o(ticker="6758.T", qty=150),                 # 単元未満
            o(ticker="9999.T", qty=100),                 # ウォッチリスト外
            o(ticker="6758.T", qty=300),                 # 銘柄比率超過
            o(ticker="7203.T", qty=200, side="sell"),    # 空売り
            o(ticker="7203.T", qty=100, side="sell"),    # OK
            o(ticker="6758.T", qty=100, rationale=" "),  # 理由なし
        ],
        pf, prices, wl, SETTINGS,
    )
    reasons = [r for _, r in results]
    assert reasons[0] is None and reasons[5] is None
    assert all(r is not None for i, r in enumerate(reasons) if i not in (0, 5))

    # ドローダウン超過中は買いだけ止まる
    pf.peak_equity = 2_000_000
    [(_, buy), (_, sell)] = check_orders(
        [o(ticker="6758.T", qty=100), o(ticker="7203.T", qty=100, side="sell")], pf, prices, wl, SETTINGS
    )
    assert "ドローダウン" in buy and sell is None

    [(_, r)] = check_orders([o(ticker="6758.T", qty=100)], pf, prices, wl, SETTINGS, stopped=True)
    assert "STOP" in r


def test_daily_cycle(root):
    frames = {t: bars(np.linspace(1000, 1500, 260)) for t in ["7203.T", "6758.T", "8306.T"]}
    d0 = frames["7203.T"].index[-2].date()
    data = FakeData({t: df.iloc[:-1] for t, df in frames.items()})

    snap = cli.cmd_prep(root, data, d0)
    assert snap["fresh"] and len(snap["watchlist"]) == 3 and snap["account"]["equity"] == 1_000_000

    orders = root / "orders.json"
    orders.write_text(json.dumps({"orders": [
        {"ticker": "7203.T", "side": "buy", "qty": 100, "type": "market", "rationale": "trend"},
        {"ticker": "7203.T", "side": "sell", "qty": 100, "type": "market", "rationale": "x"},
    ]}))
    res = cli.cmd_submit(root, data, d0, orders, force=False)
    assert len(res["accepted"]) == 1 and len(res["rejected"]) == 1
    with pytest.raises(SystemExit):
        cli.cmd_submit(root, data, d0, orders, force=False)  # 二重発注防止

    d1 = frames["7203.T"].index[-1].date()
    snap = cli.cmd_prep(root, FakeData(frames), d1)
    [ev] = snap["settled_orders"]
    assert ev["status"] == "filled" and ev["date"] == d1.isoformat()
    assert snap["positions"][0]["qty"] == 100
    assert (root / "state" / "trades.csv").read_text().count("\n") == 3  # header + rejected + filled


def test_yahoo_frame_selection():
    from trader.data import _select

    df = bars([1, 2, 3])
    by_ticker = pd.concat({"A": df, "B": df * 2}, axis=1)
    by_field = by_ticker.swaplevel(axis=1)
    assert _select(by_ticker, "B")["Close"].iloc[-1] == 6
    assert _select(by_field, "B")["Close"].iloc[-1] == 6
    assert _select(by_ticker, "C") is None
