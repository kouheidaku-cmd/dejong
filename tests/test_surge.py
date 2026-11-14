"""surge のテスト。合成データだけを使い、ネットワークには出ない。"""
from __future__ import annotations

import dataclasses
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
import yaml

from surge import backtest, data, plan as plan_mod, strategy
from surge.account import Account, load_account
from surge.cli import load_config
from surge.data import Panel
from surge.strategy import Holding, Params

P = Params(lookbacks=(20, 40, 60), entry_sma=50, exit_sma=50, regime_sma=50, high_window=40, min_history=70,
           liquid_frac=1.0, markets=("USD", "JPY"), top_n=3, hold_rank=5, near_high=0.15)


def make_panel(n=400, us=8, jp=4, seed=0, drift=None, vol=0.01, groups=None, index_drift=0.0005) -> Panel:
    rng = np.random.default_rng(seed)
    tickers = [f"U{k}" for k in range(us)] + [f"{1000 + k}.T" for k in range(jp)]
    m = len(tickers)
    drift = np.linspace(0.003, -0.001, m) if drift is None else np.asarray(drift, float)
    close = 100 * np.exp(np.cumsum(rng.normal(drift, vol, (n, m)), axis=0))
    open_ = np.vstack([close[:1], close[:-1]]) * (1 + rng.normal(0, 0.002, (n, m)))   # 始値は前日終値の近く
    fx = np.where([t.endswith(".T") for t in tickers], 1.0, 150.0) * np.ones((n, m))
    dv = np.tile(np.arange(m, 0, -1) * 1e9, (n, 1)).astype(float)
    idx = 1000 * np.exp(np.arange(n) * index_drift)
    group_of = {t: (groups or {}).get(t, t) for t in tickers}
    return Panel(pd.bdate_range("2022-01-03", periods=n), tickers, close, open_, close.copy(), dv, fx,
                 {"USD": idx.copy(), "JPY": idx.copy()}, group_of)


# ---------------------------------------------------------------- 先読みしない

def test_features_do_not_look_ahead():
    panel = make_panel()
    full = strategy.compute_features(panel, P)
    cut = 300
    part = strategy.compute_features(panel.upto(cut), P)
    for name in ("score", "rank", "eligible", "exit_trend", "risk_on", "size_mult"):
        a, b = getattr(full, name)[:cut], getattr(part, name)
        assert np.array_equal(a, b, equal_nan=True), name
    # 判定日だけは「次の日が別の週か」で決まるので、最終行を除いて一致する
    assert np.array_equal(full.rebalance_day[:cut - 1], part.rebalance_day[:cut - 1])


def test_backtest_does_not_depend_on_future_prices():
    panel = make_panel()
    cut = 300
    changed = make_panel()
    changed.close[cut:] *= 3.0
    changed.open[cut:] *= 3.0
    end = str(panel.dates[cut - 1].date())
    a = backtest.run(panel, P, end=end)
    b = backtest.run(changed, P, end=end)
    assert np.allclose(a.equity.to_numpy(), b.equity.to_numpy())
    assert len(a.trades) == len(b.trades)


# ---------------------------------------------------------------- 約定と資金

def test_orders_fill_at_next_open_with_cost():
    panel = make_panel()
    feat = strategy.compute_features(panel, P)
    r = backtest.run(panel, P, cost=0.01, integer=False, feat=feat)
    assert len(r.trades) > 0
    first = r.trades.sort_values("entry").iloc[0]
    j = panel.col(first["ticker"])
    i = int(panel.dates.searchsorted(pd.Timestamp(first["entry"])))
    assert feat.rebalance_day[i - 1], "買いは判定日の翌営業日に約定する"
    assert feat.eligible[i - 1, j] and feat.rank[i - 1, j] <= P.hold_rank


def test_never_spends_more_than_cash():
    panel = make_panel(seed=3, vol=0.03)
    for integer in (True, False):
        r = backtest.run(panel, P, capital=400_000, integer=integer)
        assert r.exposure.max() <= 1.0 + 1e-9      # 株の割合が100%を超えない = 現金がマイナスにならない
        assert (r.equity > 0).all()


def test_costs_reduce_returns():
    panel = make_panel(seed=1)
    free = backtest.run(panel, P, cost=0.0, integer=False).equity.iloc[-1]
    paid = backtest.run(panel, P, cost=0.01, integer=False).equity.iloc[-1]
    assert paid < free


def test_size_order_rounding_and_caps():
    assert backtest.size_order(76_000, 50_000, 99_000, 500_000, True) == 1      # 2株は上限を超える
    assert backtest.size_order(76_000, 30_000, 99_000, 500_000, True) == 3      # 2.5株 → 近い方 (上限内)
    assert backtest.size_order(76_000, 30_000, 99_000, 70_000, True) == 2       # 現金が足りない
    assert backtest.size_order(76_000, 120_000, 99_000, 500_000, True) == 0     # 1株も買えない
    assert backtest.size_order(76_000, 50_000, 99_000, 500_000, False) == pytest.approx(1.52)


# ---------------------------------------------------------------- ルール

def test_regime_blocks_new_buys():
    panel = make_panel(index_drift=-0.001)      # 指数が下がり続ける → 長期線の下
    r = backtest.run(panel, P)
    assert len(r.trades) == 0 and r.exposure.max() == 0.0


def test_regime_filter_can_be_turned_off():
    panel = make_panel(index_drift=-0.001)
    r = backtest.run(panel, dataclasses.replace(P, regime_sma=None))
    assert r.exposure.max() > 0.0


def test_group_cap():
    panel = make_panel()
    feat = strategy.compute_features(panel, P)
    i = int(np.flatnonzero(feat.rebalance_day)[-1])
    top3 = {int(j) for j in np.argsort(feat.rank[i])[:3]}
    assert all(feat.eligible[i, j] for j in top3)
    for j in top3:                                   # 上位3銘柄を同じ group にする
        panel.group_of[panel.tickers[j]] = "g"
    d = strategy.decide(panel, feat, i, {}, dataclasses.replace(P, max_per_group=2))
    assert len(d.buys) == 3 and len(set(d.buys) & top3) == 2


def test_trailing_stop_fires_on_any_day():
    panel = make_panel()
    feat = strategy.compute_features(panel, P)
    i = int(np.flatnonzero(~feat.rebalance_day[:-1])[-1])     # 判定日ではない日
    panel.close[i, 0] = panel.close[i - 30:i, 0].max() * 0.7     # 高値から -30%
    d = strategy.decide(panel, feat, i, {0: Holding(entry_i=i - 30, shares=10)}, P)
    assert [j for j, _ in d.sells] == [0] and d.sells[0][1].startswith("trail_stop")
    assert d.buys == []     # 入れ替えは週次だけ


def test_rank_exit_and_replacement():
    panel = make_panel()
    feat = strategy.compute_features(panel, P)
    i = int(np.flatnonzero(feat.rebalance_day)[-1])
    worst = int(np.argsort(feat.rank[i])[7])          # 持ち続けてよい順位 (5位) より下
    best = int(np.argsort(feat.rank[i])[0])
    held = {worst: Holding(i - 5, 10), best: Holding(i - 5, 10)}
    d = strategy.decide(panel, feat, i, held, dataclasses.replace(P, trail_stop=None), weights={worst: 0.3, best: 0.3})
    assert [j for j, _ in d.sells] == [worst]
    assert best not in d.buys and len(d.buys) == 2     # 3枠のうち1つは持ち続け、2つを新しく買う


def test_trim_and_topup():
    panel = make_panel()
    feat = strategy.compute_features(panel, P)
    i = int(np.flatnonzero(feat.rebalance_day)[-1])
    a, b = (int(x) for x in np.argsort(feat.rank[i])[:2])
    held = {a: Holding(i - 5, 10), b: Holding(i - 5, 10)}
    d = strategy.decide(panel, feat, i, held, P, weights={a: 0.50, b: 0.05})
    assert d.trims == [a] and d.adds == [b] and d.sells == []


def test_markets_restrict_buys():
    panel = make_panel(drift=np.linspace(-0.001, 0.003, 12))   # 日本株のほうが強い
    r = backtest.run(panel, dataclasses.replace(P, markets=("USD",)))
    assert not r.trades["ticker"].str.endswith(".T").any()


# ---------------------------------------------------------------- データ

def _frame(dates, price):
    p = pd.Series(price, index=pd.DatetimeIndex(dates), dtype=float)
    return pd.DataFrame({"Open": p * 0.99, "High": p, "Low": p * 0.98, "Close": p, "Adj Close": p * 0.5, "Volume": 1000.0})


def test_build_panel_aligns_holidays():
    us_days = pd.to_datetime(["2024-01-02", "2024-01-03", "2024-01-04"])
    jp_days = pd.to_datetime(["2024-01-04", "2024-01-05"])       # 日本は 1/4 が大発会
    frames = {"AAA": _frame(us_days, [10, 11, 12]), "1111.T": _frame(jp_days, [100, 101]),
              "^GSPC": _frame(us_days, [1, 1, 1]), "^N225": _frame(jp_days, [1, 1]),
              "JPY=X": _frame(us_days.union(jp_days), [150, 151, 152, 153])}
    uni = {"tickers": ["1111.T", "AAA"], "group_of": {"AAA": "a", "1111.T": "b"},
           "indices": {"USD": "^GSPC", "JPY": "^N225"}, "fx": {"USD": "JPY=X"}}
    panel = data.build_panel(frames, uni)
    assert list(panel.dates.strftime("%m-%d")) == ["01-02", "01-03", "01-04", "01-05"]
    a, j = panel.col("AAA"), panel.col("1111.T")
    assert np.isnan(panel.open[3, a]) and panel.close[3, a] == 6.0        # 休場日: 始値なし、終値は前の値 (調整後)
    assert np.isnan(panel.close[0, j]) and panel.raw_close[2, j] == 100
    assert panel.open[2, a] == pytest.approx(12 * 0.99 * 0.5)             # 始値にも配当の調整がかかる
    assert panel.fx[1, a] == 151 and panel.fx[1, j] == 1.0


def test_drop_incomplete_bar():
    df = _frame(pd.to_datetime(["2026-11-12", "2026-11-13"]), [1, 2])
    during = pd.Timestamp("2026-11-13 16:30", tz="UTC")        # NY 11:30、取引時間中
    after = pd.Timestamp("2026-11-13 21:30", tz="UTC")         # NY 16:30、引け後
    assert len(data.drop_incomplete(df, "MSFT", during)) == 1
    assert len(data.drop_incomplete(df, "MSFT", after)) == 2
    assert len(data.drop_incomplete(df, "6758.T", during)) == 2   # 東京は 14日 1:30 で、13日の足は確定済み
    assert len(data.drop_incomplete(df, "JPY=X", during)) == 2


def test_universe_file():
    u = data.load_universe()
    assert all(isinstance(t, str) for t in u["tickers"])          # YAML が ON を true と読む事故の検出
    assert len(u["tickers"]) == len(set(u["tickers"])) > 200
    assert "ON" in u["tickers"] and "6758.T" in u["tickers"]


def test_config_matches_code_defaults():
    assert load_config()["params"] == Params()


# ---------------------------------------------------------------- 注文案

def _account(positions, cash=100_000.0, stopped=False) -> Account:
    return Account(cash=cash, positions=positions, other_assets=0.0, max_position_pct=0.25, min_cash_pct=0.05,
                   nisa_remaining=None, stopped=stopped)


def _positions(panel, feat, i):
    """順位が一番下の銘柄と、母集団にない銘柄を持っている口座。"""
    worst = panel.tickers[int(np.argsort(feat.rank[i])[-1])]
    cur = "JPY" if worst.endswith(".T") else "USD"
    return worst, {worst: {"qty": 3, "avg_cost": 100.0, "currency": cur, "opened": str(panel.dates[i - 20].date())},
                   "ZZZZ": {"qty": 1, "avg_cost": 1000.0, "currency": "JPY", "opened": "2022-01-04"}}


def test_plan_sells_weak_and_buys_strong_within_limits():
    panel = make_panel(seed=5)
    p = dataclasses.replace(P, top_n=5, hold_rank=8)
    feat = strategy.compute_features(panel, p)
    i = len(panel.dates) - 1
    worst, positions = _positions(panel, feat, i)
    acct = _account(positions, cash=400_000.0)
    out = plan_mod.make_plan(panel, p, acct, {"price_buffer": 0.03, "max_weight_mult": 1.3}, rebalance=True, today=date(2023, 7, 15))
    sells = [o for o in out["orders"] if o["side"] == "sell"]
    buys = [o for o in out["orders"] if o["side"] == "buy"]
    assert [o["ticker"] for o in sells] == [worst] and sells[0]["qty"] == 3
    assert 1 <= len(buys) <= 5
    for o in buys:
        assert o["qty"] == int(o["qty"]) > 0
        assert o["amount_jpy"] <= 0.25 * out["equity"] + 1          # 1銘柄 25% まで
        assert feat.eligible[i, panel.col(o["ticker"])]
    assert out["cash_after"] >= 0.05 * out["equity"] * 0.9           # 現金 5% を残す (価格の余裕ぶんの誤差は許す)
    assert any("ZZZZ" in w for w in out["warnings"])
    assert "注文案" in plan_mod.render(out)


def test_plan_daily_mode_only_checks_stops():
    panel = make_panel(seed=5)
    feat = strategy.compute_features(panel, P)
    i = len(panel.dates) - 1
    _, positions = _positions(panel, feat, i)
    out = plan_mod.make_plan(panel, P, _account(positions), {}, rebalance=False, today=date(2023, 7, 14))
    assert out["mode"] == "daily" and all(o["side"] == "sell" and o["reason"].startswith("trail_stop") for o in out["orders"])


def test_plan_exclude_moves_to_next_candidate_and_stop_cancels():
    panel = make_panel(seed=5)
    cfg = {"price_buffer": 0.03, "max_weight_mult": 1.3}
    base = plan_mod.make_plan(panel, P, _account({}, cash=400_000.0), cfg, rebalance=True, today=date(2023, 7, 15))
    first = next(o["ticker"] for o in base["orders"] if o["side"] == "buy")
    out = plan_mod.make_plan(panel, P, _account({}, cash=400_000.0), cfg, rebalance=True, exclude=(first,), today=date(2023, 7, 15))
    bought = [o["ticker"] for o in out["orders"]]
    assert first not in bought and len(bought) == len(base["orders"])
    stopped = plan_mod.make_plan(panel, P, _account({}, cash=400_000.0, stopped=True), cfg, rebalance=True, today=date(2023, 7, 15))
    assert stopped["orders"] == [] and any("STOP" in w for w in stopped["warnings"])


def test_plan_matches_backtest_decision():
    """注文案の買い候補は、検証と同じ decide() の結果になる。"""
    panel = make_panel(seed=7)
    feat = strategy.compute_features(panel, P)
    i = len(panel.dates) - 1
    d = strategy.decide(panel, feat, i, {}, P, rebalance=True)
    out = plan_mod.make_plan(panel, P, _account({}, cash=10_000_000.0), {}, rebalance=True, today=date(2023, 7, 15))
    assert [o["ticker"] for o in out["orders"]] == [panel.tickers[j] for j in d.buys]


def test_expected_sessions():
    sat_early = pd.Timestamp("2026-11-13 18:30", tz="UTC")     # 日本時間 土曜 3:30 (米国は金曜の取引時間中)
    sat_late = pd.Timestamp("2026-11-14 00:45", tz="UTC")      # 日本時間 土曜 9:45
    wed = pd.Timestamp("2026-11-11 07:19", tz="UTC")           # 日本時間 水曜 16:19
    assert plan_mod.expected_sessions(sat_early) == {"JPY": date(2026, 11, 13), "USD": date(2026, 11, 12)}
    assert plan_mod.expected_sessions(sat_late) == {"JPY": date(2026, 11, 13), "USD": date(2026, 11, 13)}
    assert plan_mod.expected_sessions(wed) == {"JPY": date(2026, 11, 11), "USD": date(2026, 11, 10)}


def test_load_account_reads_shared_state(tmp_path: Path):
    (tmp_path / "state").mkdir()
    (tmp_path / "config").mkdir()
    (tmp_path / "state" / "portfolio.json").write_text(
        '{"cash": 1000, "positions": {"MSFT": {"qty": 1, "avg_cost": 400, "currency": "USD", "opened": "2026-10-20"}}, "other_assets": 5}')
    (tmp_path / "config" / "settings.yaml").write_text(yaml.safe_dump(
        {"risk": {"max_position_pct": 0.2, "min_cash_pct": 0.1}, "nisa": {"year": 2026, "as_of": "2026-10-22", "growth_remaining": 100000}}))
    (tmp_path / "state" / "trades.csv").write_text(
        "date,id,ticker,side,qty,type,limit_price,status,price,currency,fx,commission,realized_pnl,rationale\n"
        "2026-10-21,,AAA,buy,1,,,filled,100,USD,150,0,,\n"        # as_of より前: 数えない
        "2026-10-23,,BBB,buy,2,,,filled,100,USD,150,0,,\n"        # 30,000円
        "2026-10-23,,CCC,sell,2,,,filled,100,USD,150,0,,\n"
        "2026-10-24,1,DDD,buy,2,,,proposal_unconfirmed,,,,,,\n")
    a = load_account(tmp_path)
    assert (a.cash, a.max_position_pct, a.min_cash_pct, a.stopped) == (1000.0, 0.2, 0.1, False)
    assert a.nisa_remaining == 70000
    (tmp_path / "STOP").write_text("")
    assert load_account(tmp_path).stopped
