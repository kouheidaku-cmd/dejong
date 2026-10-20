import json
import shutil
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
import yaml

from trader import cli, config, indicators
from trader.broker.paper import PaperBroker
from trader.portfolio import Portfolio
from trader.risk import check_orders

ROOT = Path(__file__).resolve().parent.parent
SETTINGS = config.load_settings(ROOT)
FX = {"JPY": 1.0, "USD": 150.0}


def bars(closes, start="2026-01-05"):
    idx = pd.bdate_range(start, periods=len(closes))
    c = pd.Series(closes, index=idx, dtype=float)
    return pd.DataFrame({"Open": c, "High": c * 1.01, "Low": c * 0.99, "Close": c, "Volume": 1000.0})


class FakeData:
    def __init__(self, frames):
        self.frames = frames

    def history(self, tickers, days):
        return {t: self.frames[t] for t in tickers if t in self.frames}


def make_root(tmp_path, broker, sync=None):
    shutil.copytree(ROOT / "config", tmp_path / "config")
    settings = config.load_yaml(tmp_path / "config" / "settings.yaml")
    settings["broker"] = broker
    (tmp_path / "config" / "settings.yaml").write_text(yaml.safe_dump(settings, allow_unicode=True))
    (tmp_path / "config" / "watchlist.yaml").write_text(yaml.safe_dump(
        {"tickers": [{"ticker": "7203.T", "name": "A"}, {"ticker": "MSFT", "name": "B"}]}))
    src = None
    if sync:
        src = tmp_path / "sync.yaml"
        src.write_text(yaml.safe_dump(sync))
    cli.cmd_init(tmp_path, 1_000_000 if not sync else None, src, force=False)
    return tmp_path


def write_orders(root, orders):
    path = root / "orders.json"
    path.write_text(json.dumps({"orders": orders}))
    return path


def test_indicators_on_uptrend():
    s = indicators.summarize(bars(np.linspace(100, 200, 250)))
    assert s["ret_60d"] > 0 and s["rsi14"] > 70
    assert indicators.trend_ok(s)
    ranked = indicators.screen({"UP": s, "DOWN": indicators.summarize(bars(np.linspace(200, 100, 250)))})
    assert [r["ticker"] for r in ranked] == ["UP", "DOWN"]


def test_paper_fill_next_open_in_base_currency():
    pf = Portfolio(cash=100_000)
    b = PaperBroker(pf, {**SETTINGS, "execution": {}})
    df = bars([100, 110, 120], start="2026-03-02")
    b.submit({"ticker": "MSFT", "side": "buy", "qty": 2}, date(2026, 3, 2))
    assert b.settle({"MSFT": df.iloc[:1]}, FX, date(2026, 3, 2)) == []  # 翌日の足がまだない → 持ち越し
    [ev] = b.settle({"MSFT": df}, FX, date(2026, 3, 3))
    assert ev["status"] == "filled" and ev["price"] == 110 and ev["date"] == "2026-03-03"
    assert pf.cash == 100_000 - 2 * 110 * 150
    assert pf.positions["MSFT"] == {"qty": 2, "avg_cost": 110, "currency": "USD", "opened": "2026-03-03", "took_profit": False}

    b.submit({"ticker": "MSFT", "side": "sell", "qty": 2}, date(2026, 3, 3))
    [ev] = b.settle({"MSFT": df}, FX, date(2026, 3, 4))
    assert ev["realized_pnl"] == 2 * 10 * 150 and "MSFT" not in pf.positions


def test_limit_order_expires_when_not_touched():
    pf = Portfolio(cash=100_000)
    b = PaperBroker(pf, SETTINGS)
    b.submit({"ticker": "X", "side": "buy", "qty": 1, "type": "limit", "limit_price": 50}, date(2026, 3, 2))
    [ev] = b.settle({"X": bars([100, 100], start="2026-03-02")}, FX, date(2026, 3, 3))
    assert ev["status"] == "expired" and pf.cash == 100_000 and not pf.pending


def test_risk_rules():
    pf = Portfolio(cash=200_000, positions={
        "7203.T": {"qty": 100, "avg_cost": 2000, "currency": "JPY"},
        "MSFT": {"qty": 2, "avg_cost": 400, "currency": "USD"},
    }, peak_equity=500_000)
    # 総資産 = 200,000 + 200,000 + 2*500*150 = 550,000
    prices = {"7203.T": 2000.0, "6758.T": 1000.0, "MSFT": 500.0, "9999.T": 500.0}
    wl = {"7203.T", "6758.T", "MSFT"}
    o = lambda **kw: {"side": "buy", "type": "market", "rationale": "test", **kw}
    results = check_orders(
        [
            o(ticker="6758.T", qty=50),                  # OK (1株単位)
            o(ticker="9999.T", qty=10),                  # ウォッチリスト外
            o(ticker="6758.T", qty=200),                 # 銘柄比率超過
            o(ticker="MSFT", qty=1),                     # MSFT は既に 27% → 超過
            o(ticker="7203.T", qty=200, side="sell"),    # 空売り
            o(ticker="7203.T", qty=50, side="sell"),     # OK
            o(ticker="6758.T", qty=10, rationale=" "),   # 理由なし
        ],
        pf, prices, FX, wl, SETTINGS,
    )
    reasons = [r for _, r in results]
    assert reasons[0] is None and reasons[5] is None, reasons
    assert all(r is not None for i, r in enumerate(reasons) if i not in (0, 5)), reasons

    # 現金ゼロでも同じ日の売りの代金で買える (注文の並び順によらない)
    pf.cash, pf.peak_equity = 0, 350_000
    [(_, buy), (_, sell)] = check_orders(
        [o(ticker="6758.T", qty=50), o(ticker="7203.T", qty=100, side="sell")], pf, prices, FX, wl, SETTINGS
    )
    assert buy is None and sell is None

    # ドローダウン超過中は買いだけ止まる
    pf.cash, pf.peak_equity = 200_000, 1_000_000
    [(_, buy), (_, sell)] = check_orders(
        [o(ticker="6758.T", qty=10), o(ticker="7203.T", qty=100, side="sell")], pf, prices, FX, wl, SETTINGS
    )
    assert "ドローダウン" in buy and sell is None

    [(_, r)] = check_orders([o(ticker="6758.T", qty=10)], pf, prices, FX, wl, SETTINGS, stopped=True)
    assert "STOP" in r


def frames_for(n=260):
    f = {t: bars(np.linspace(1000, 1500, n)) for t in ["7203.T"]}
    f["MSFT"] = bars(np.linspace(400, 500, n))
    f["JPY=X"] = bars(np.full(n, 150.0))
    return f


def test_paper_daily_cycle(tmp_path):
    root = make_root(tmp_path, "paper")
    frames = frames_for()
    d0 = frames["7203.T"].index[-2].date()
    data = FakeData({t: df.iloc[:-1] for t, df in frames.items()})

    snap = cli.cmd_prep(root, data, d0)
    assert snap["latest_bar_date"]["JPY"] == d0.isoformat() and snap["account"]["equity"] == 1_000_000

    orders = write_orders(root, [
        {"ticker": "7203.T", "side": "buy", "qty": 100, "type": "market", "rationale": "trend"},
        {"ticker": "7203.T", "side": "sell", "qty": 100, "type": "market", "rationale": "x"},
    ])
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


def test_manual_cycle_record_and_sync(tmp_path):
    root = make_root(tmp_path, "manual", sync={
        "cash_jpy": 452, "other_assets_jpy": 20410,
        "positions": {"7203.T": {"qty": 15, "avg_cost": 1100}, "MSFT": {"qty": 2, "avg_cost": 386.75}},
    })
    frames = frames_for()
    d0 = frames["7203.T"].index[-1].date()
    data = FakeData(frames)

    snap = cli.cmd_prep(root, data, d0)
    assert snap["account"]["equity"] == round(452 + 15 * 1500 + 2 * 500 * 150)
    assert snap["account"]["total_assets"] == snap["account"]["equity"] + 20410

    orders = write_orders(root, [
        {"ticker": "MSFT", "side": "sell", "qty": 1, "type": "market", "rationale": "比率を下げる"},
        {"ticker": "7203.T", "side": "buy", "qty": 10, "type": "market", "rationale": "売却代金で買う"},
    ])
    res = cli.cmd_submit(root, data, d0, orders, force=False)
    assert [o["status"] for o in res["accepted"]] == ["proposed", "proposed"]
    pf = Portfolio.load(root / "state" / "portfolio.json")
    assert pf.qty("MSFT") == 2  # 注文案を出しただけでは口座は変わらない

    out = cli.cmd_record(root, data, d0, "MSFT", "sell", 1, 510.0, 151.0, 0.0)
    assert out["event"]["realized_pnl"] == round((510 - 386.75) * 151, 2)
    pf = Portfolio.load(root / "state" / "portfolio.json")
    assert pf.qty("MSFT") == 1 and len(pf.pending) == 1

    # 報告のなかった注文案は翌日の prep で未確認として流れる
    d1 = pd.Timestamp(d0) + pd.offsets.BDay(1)
    snap = cli.cmd_prep(root, data, d1.date())
    assert [e["status"] for e in snap["settled_orders"]] == ["proposal_unconfirmed"]

    src = root / "sync2.yaml"
    src.write_text(yaml.safe_dump({"cash_jpy": 1000, "positions": {"7203.T": {"qty": 25, "avg_cost": 1200}}}))
    out = cli.cmd_sync(root, d1.date(), src)
    assert {(c["ticker"], c["side"], c["qty"]) for c in out["position_changes"]} == {("7203.T", "buy", 10), ("MSFT", "sell", 1)}


def test_yahoo_frame_selection():
    from trader.data import _select

    df = bars([1, 2, 3])
    by_ticker = pd.concat({"A": df, "B": df * 2}, axis=1)
    by_field = by_ticker.swaplevel(axis=1)
    assert _select(by_ticker, "B")["Close"].iloc[-1] == 6
    assert _select(by_field, "B")["Close"].iloc[-1] == 6
    assert _select(by_ticker, "C") is None


RULES = config.load_yaml(ROOT / "config" / "rules.yaml")


def test_rules_entry_and_exit():
    row = {"close": 110, "sma20": 105, "sma50": 100, "sma200": 90, "rsi14": 60, "ret_60d": 0.1}
    from trader import rules
    assert rules.entry_check(row, RULES) == []
    assert rules.entry_check({**row, "rsi14": 80}, RULES)  # 過熱
    assert rules.entry_check({**row, "close": 95}, RULES)  # トレンド割れ
    assert rules.entry_check({**row, "sma20": float("nan")}, RULES) == ["データ不足"]

    ex = lambda **kw: rules.exit_check({**row, **kw}, 100, kw.pop("peak", 110), 10, False, RULES)
    assert ex(close=91) == ("stop_loss", 1.0)
    assert ex(close=101, peak=125) == ("trailing_stop", 1.0)
    assert ex(close=125) == ("take_profit", 0.5)
    assert ex(close=105) is None
    assert rules.sell_qty(1, 0.5) == 1 and rules.sell_qty(15, 0.5) == 7


def rise_then_crash(n_up=300, n_down=60):
    up = np.linspace(100, 200, n_up)
    down = np.linspace(200, 120, n_down)
    return bars(np.concatenate([up, down]), start="2018-01-01")


def test_backtest_trades_and_loss_analysis():
    from trader import backtest
    frames = {"UP": rise_then_crash(), "DOWN": bars(np.linspace(200, 100, 360), start="2018-01-01")}
    start, end = frames["UP"].index[220].date(), frames["UP"].index[-1].date()
    r = backtest.run(frames, RULES, start, end, initial_cash=1_000_000, slippage_bps=0)
    tickers = {t["ticker"] for t in r["trades"]}
    assert tickers == {"UP"}  # 下落トレンドの銘柄は買わない
    first = r["trades"][0]
    # 約定は「シグナルの翌日の寄付き」: 入口価格はエントリー日の始値
    assert first["entry_price"] == pytest.approx(float(frames["UP"].loc[first["entry_date"], "Open"]), rel=1e-6)
    reasons = {t["exit_reason"] for t in r["trades"]}
    assert reasons & {"take_profit", "trailing_stop", "trend_break", "stop_loss"}
    assert r["metrics"]["trades"] == len(r["trades"]) and r["metrics"]["max_drawdown"] >= 0
    assert set(r["analysis"]["by_exit_reason"]) == reasons
    assert r["benchmark"]["total_return"] is not None


def test_backtest_command_with_fx(tmp_path):
    root = make_root(tmp_path, "paper")
    frames = {"7203.T": rise_then_crash(), "MSFT": rise_then_crash(), "JPY=X": bars(np.full(360, 150.0), start="2018-01-01")}
    out = cli.cmd_backtest(root, FakeData(frames), root / "config" / "rules.yaml", 1, None, ["7203.T", "MSFT"], "t")
    assert out["tickers"] == 2
    assert out["in_sample"]["metrics"] and out["out_of_sample"]["period"]["start"] > out["in_sample"]["period"]["end"]
    assert (root / "reports" / "backtest" / "t" / "result.json").exists()


def test_prep_reports_rule_signals(tmp_path):
    root = make_root(tmp_path, "manual", sync={
        "date": "2026-01-05", "cash_jpy": 0,
        "positions": {"7203.T": {"qty": 10, "avg_cost": 2000}, "MSFT": {"qty": 1, "avg_cost": 400}},
    })
    frames = frames_for()
    snap = cli.cmd_prep(root, FakeData(frames), frames["7203.T"].index[-1].date())
    sig = {e["ticker"]: e for e in snap["rule_signals"]["exits"]}
    assert sig["7203.T"]["signal"] == "stop_loss" and sig["7203.T"]["sell_qty"] == 10  # 1500 / 2000 = -25%
    assert sig["MSFT"]["signal"] == "take_profit"  # 500 / 400 = +25%
    assert snap["rule_signals"]["open_slots"] == 5  # 1株の利確は全売却になるので2枠とも空く
