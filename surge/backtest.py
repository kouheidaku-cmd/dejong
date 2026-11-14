"""過去の日足での仮想売買。

実際の運用に合わせた作り:
  - 判定は終値、約定は翌営業日の寄付き (その市場が休みなら次に開く日)
  - 円の現金ひとつで日米の株を持つ。米国株は毎日の為替で円換算する
  - 1日の中の順番は 日本の買い → 日本の売り → 米国の買い → 米国の売り。
    売った代金は、その後に開く市場の買いにしか使えない (日本の寄付きで売った代金は、その夜の米国の買いに使える)
  - 株数は整数 (integer=True)。口座が小さいと1株が高い銘柄は買えない
  - 売買のたびに cost (片道) を引く
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from .data import Panel, currency
from .strategy import Params, Features, Holding, compute_features, decide

HORIZON = 87   # 4か月 (日米の営業日の和集合で約 260日/年)
YEAR = 260


@dataclass
class Result:
    equity: pd.Series
    trades: pd.DataFrame      # 1往復 = 1行
    exposure: pd.Series       # 株の割合
    params: Params

    def stats(self) -> dict:
        return {**stats(self.equity), "trades": int(len(self.trades)),
                "win_rate": float((self.trades["ret"] > 0).mean()) if len(self.trades) else None,
                "avg_exposure": float(self.exposure.mean())}


def size_order(budget: float, px_jpy: float, cap: float, cash_avail: float, integer: bool) -> float:
    """budget 円ぶん買うときの株数。整数株では近い方に丸めるが、cap (1銘柄の上限額) と使える現金は超えない。"""
    if px_jpy <= 0 or budget <= 0:
        return 0.0
    if not integer:
        return min(budget, cash_avail) / px_jpy
    q = np.floor(min(budget, cash_avail) / px_jpy)
    up = (q + 1) * px_jpy
    if up <= min(cap, cash_avail) and up - budget < budget - q * px_jpy:
        q += 1
    return float(q)


def run(panel: Panel, p: Params, start: str | None = None, end: str | None = None, capital: float = 400_000.0,
        cost: float = 0.002, integer: bool = True, feat: Features | None = None,
        max_weight_mult: float = 1.3, buy_patience: int = 3) -> Result:
    feat = feat or compute_features(panel, p)
    dates = panel.dates
    i0 = int(dates.searchsorted(pd.Timestamp(start))) if start else 0
    i0 = max(i0, max(p.lookbacks) + p.skip + 1)
    i1 = int(dates.searchsorted(pd.Timestamp(end), side="right")) if end else len(dates)
    is_jp = np.array([currency(t) == "JPY" for t in panel.tickers])

    cash = capital
    held: dict[int, Holding] = {}
    entry_cost: dict[int, float] = {}
    pend_sell: dict[int, tuple[float | None, str]] = {}   # 列 → (売る株数 None=全部, 理由)
    pend_buy: list[list] = []                              # [列, 残り日数, 金額の倍率]
    eq_prev = capital
    eq, expo, trades = [], [], []

    def sell(i, j, qty, reason):
        nonlocal cash
        h = held[j]
        qty = h.shares if qty is None else min(qty, h.shares)
        px = panel.open[i, j] * panel.fx[i, j]
        proceeds = qty * px * (1 - cost)
        cash += proceeds
        frac = qty / h.shares
        cost_part = entry_cost[j] * frac
        trades.append({"ticker": panel.tickers[j], "entry": dates[h.entry_i].date(), "exit": dates[i].date(),
                       "ret": proceeds / cost_part - 1.0, "pnl": proceeds - cost_part, "reason": reason.split(":")[0]})
        h.shares -= qty
        entry_cost[j] -= cost_part
        if h.shares <= 1e-9:
            del held[j], entry_cost[j]

    def buy(i, j, cash_avail, mult):
        nonlocal cash
        px = panel.open[i, j] * panel.fx[i, j] * (1 + cost)
        full = p.target_weight() * eq_prev * mult
        have = held[j].shares * px if j in held else 0.0      # 買い増しなら、目標との差だけ買う
        target = full - have
        avail = min(cash_avail, cash) - p.cash_buffer * eq_prev
        if avail < 0.5 * target:
            return False
        q = size_order(target, px, max_weight_mult * full - have, avail, integer)
        if q <= 0:
            return None      # 1株も買えない
        cash -= q * px
        if j in held:
            held[j].shares += q
            entry_cost[j] += q * px
        else:
            held[j] = Holding(entry_i=i, shares=q)
            entry_cost[j] = q * px
        return True

    for i in range(i0, i1):
        # --- 寄付き: 前日までに決めた注文を執行
        for jp in (True, False):
            cash_before = cash
            still = []
            for j, left, mult in pend_buy:
                if is_jp[j] != jp:
                    still.append([j, left, mult])
                    continue
                if np.isnan(panel.open[i, j]):
                    still.append([j, left, mult])       # 休場。期限は減らさない
                    continue
                done = buy(i, j, cash_before, mult)
                if done is False and left > 1:
                    still.append([j, left - 1, mult])   # 現金待ち
            pend_buy = still
            for j in [j for j in pend_sell if is_jp[j] == jp and not np.isnan(panel.open[i, j])]:
                qty, reason = pend_sell.pop(j)
                if j in held:
                    sell(i, j, qty, reason)
        # --- 引け: 評価
        stock = sum(h.shares * panel.close[i, j] * panel.fx[i, j] for j, h in held.items())
        eq_now = cash + stock
        eq.append(eq_now)
        expo.append(stock / eq_now if eq_now > 0 else 0.0)
        eq_prev = eq_now
        # --- 判定 (翌営業日の寄付きで執行)
        active = {j: h for j, h in held.items() if j not in pend_sell}
        weights = {j: h.shares * panel.close[i, j] * panel.fx[i, j] / eq_now for j, h in active.items()}
        target = p.target_weight() * eq_now

        def affordable(j):
            return (not integer) or panel.close[i, j] * panel.fx[i, j] <= max_weight_mult * target * feat.size_mult[i, j]

        if feat.rebalance_day[i]:
            pend_buy = []     # 前の週に買えなかった注文は捨てて、新しい順位でやり直す
        d = decide(panel, feat, i, active, p, weights=weights, affordable=affordable)
        for j, reason in d.sells:
            pend_sell[j] = (None, reason)
        for j in d.trims:
            px = panel.close[i, j] * panel.fx[i, j]
            qty = (weights[j] * eq_now - target) / px
            qty = np.floor(qty) if integer else qty
            if qty > 0:
                pend_sell[j] = (float(qty), "trim: 上限超え")
        pend_buy += [[j, buy_patience, float(feat.size_mult[i, j])] for j in d.buys + d.adds]

    idx = dates[i0:i1]
    return Result(pd.Series(eq, index=idx), pd.DataFrame(trades, columns=["ticker", "entry", "exit", "ret", "pnl", "reason"]),
                  pd.Series(expo, index=idx), p)


# ---------------------------------------------------------------- 成績

def stats(equity: pd.Series) -> dict:
    r = equity.pct_change().dropna()
    years = len(equity) / YEAR
    total = equity.iloc[-1] / equity.iloc[0] - 1.0
    dd = equity / equity.cummax() - 1.0
    roll = rolling_returns(equity)
    out = {
        "total_return": float(total),
        "cagr": float((1 + total) ** (1 / years) - 1) if years > 0 else None,
        "vol": float(r.std() * np.sqrt(YEAR)),
        "sharpe": float(r.mean() / r.std() * np.sqrt(YEAR)) if r.std() > 0 else None,
        "max_drawdown": float(-dd.min()),
    }
    if len(roll):
        out["h4m"] = {
            "median": float(roll.median()),
            "p_ge_20": float((roll >= 0.20).mean()),
            "p_ge_10": float((roll >= 0.10).mean()),
            "p_lt_0": float((roll < 0).mean()),
            "p_le_m10": float((roll <= -0.10).mean()),
            "p05": float(roll.quantile(0.05)),
            "worst": float(roll.min()),
            "best": float(roll.max()),
        }
    return out


def rolling_returns(equity: pd.Series, horizon: int = HORIZON) -> pd.Series:
    """どの日から始めても「4か月後に何%だったか」の一覧。"""
    return (equity.shift(-horizon) / equity - 1.0).dropna()


def yearly_returns(equity: pd.Series) -> pd.Series:
    last = equity.groupby(equity.index.year).last()
    first = pd.concat([equity.iloc[:1], last.iloc[:-1]]).to_numpy()
    return pd.Series(last.to_numpy() / first - 1.0, index=last.index)


def benchmarks(panel: Panel, start, end) -> dict[str, pd.Series]:
    """比べる相手 (円換算)。universe_ew は母集団を毎日均等に持ったもの (後知恵の偏りの大きさを見るため)。"""
    dates = panel.dates
    jpy = panel.close * panel.fx
    daily = pd.DataFrame(jpy, index=dates).pct_change(fill_method=None)
    ew = daily.mean(axis=1, skipna=True).fillna(0.0)
    usd_col = next(j for j, t in enumerate(panel.tickers) if currency(t) == "USD")
    idx_us = pd.Series(panel.index_close["USD"] * panel.fx[:, usd_col], index=dates).pct_change().fillna(0.0)
    idx_jp = pd.Series(panel.index_close["JPY"], index=dates).pct_change().fillna(0.0)
    out = {"universe_ew": ew, "index_5050": 0.5 * idx_us + 0.5 * idx_jp, "sp500_jpy": idx_us, "nikkei": idx_jp}
    sl = slice(pd.Timestamp(start), pd.Timestamp(end) if end else None)
    res = {}
    for k, r in out.items():
        r = r.loc[sl].copy()
        r.iloc[0] = 0.0
        res[k] = (1 + r).cumprod()
    return res
