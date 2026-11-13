"""売買ルール本体。検証 (backtest.py) と毎日の注文案 (plan.py) が、この同じ関数を使う。

考え方: 日米の大型株から「いま一番強く上がっている数銘柄」に集中して持ち、弱くなったら入れ替える。
  - 順位づけ: 過去3・6・12か月の値上がり率の平均 (モメンタム)
  - 買い: 週1回、上位 top_n 銘柄を均等に。市場の指数が長期線より下の市場では新規に買わない
  - 売り: 順位が hold_rank より下に落ちた / 自分の長期線を割った (週1回)、高値から trail_stop 下げた (毎日)
どの判定も「その日の終値まで」しか使わない。約定は翌営業日の寄付き。
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict

import numpy as np
import pandas as pd

from .data import Panel, currency


@dataclass(frozen=True)
class Params:
    # 順位づけ
    score: str = "blend"            # blend: 値上がり率の平均 / riskadj: それを値動きの大きさで割る / clenow: 回帰の傾き×決定係数
    lookbacks: tuple = (63, 126, 252)
    skip: int = 0                   # 直近この日数を値上がり率の計算から外す (短期の反動を避ける)
    # 銘柄を選ぶ
    top_n: int = 5                  # 同時に持つ銘柄数
    hold_rank: int = 20             # 保有中の銘柄は、この順位以内なら持ち続ける
    entry_sma: int = 200            # 終値がこの移動平均より上の銘柄だけ買う
    exit_sma: int | None = 200      # 終値がこの移動平均を割ったら売る (週次)
    max_per_group: int = 2          # 同じ group は何銘柄まで
    liquid_frac: float = 0.6        # 売買代金 (60日平均) が、その市場の母集団の上位この割合に入る銘柄だけ
                                    # (「今は大型株だが当時は小さかった」銘柄を、過去の検証で買わないため)
    min_history: int = 280          # 上場からこの日数たっていない銘柄は対象外
    markets: tuple = ("USD",)       # 順位づけの対象にする市場 (通貨で指定)。日本株はモメンタムが効きにくいので外している
    near_high: float | None = 0.15  # 直近 high_window 日の最高終値からこの割合以内にいる銘柄だけ買う (落ちている途中の銘柄を買わない)
    high_window: int = 126
    # 地合い
    regime_sma: int | None = 200    # 市場の指数がこの移動平均より下なら、その市場では新規に買わない
    regime_exit: bool = False       # True なら、指数が下回った市場の保有も売る
    breadth_min: float | None = None  # その市場で長期線より上にいる銘柄の割合がこれ未満なら、新規に買わない
    # 毎日の売り
    trail_stop: float | None = 0.25  # 買ってからの最高終値からこの割合下げたら売る
    # 資金
    cash_buffer: float = 0.05       # 現金で残す割合
    vol_cap: float | None = None    # 値動き (年率) がこれより大きい銘柄は、その分だけ買う金額を減らす
    vol_window: int = 63
    topup_below: float | None = 0.6  # 持ち続ける銘柄が目標の金額のこの割合より小さく、今も買いの条件を満たすなら、目標まで買い増す
    trim_above: float | None = 0.35  # 1銘柄がこの割合を超えたら、目標の割合まで減らす (None なら伸ばしっぱなし)
    rebalance: str = "W"            # W: 週の最後の営業日に判定 / M: 月の最後の営業日
    rebalance_every: int = 1        # W のとき、何週間ごとに入れ替えるか
    rebalance_offset: int = 0       # 何週目に当てるか (0 〜 rebalance_every-1)。週の番号は 1970-01-05 の週を 0 とする

    def target_weight(self) -> float:
        return (1.0 - self.cash_buffer) / self.top_n

    def to_dict(self) -> dict:
        d = asdict(self)
        d["lookbacks"] = list(self.lookbacks)
        d["markets"] = list(self.markets)
        return d

    @staticmethod
    def from_dict(d: dict) -> "Params":
        d = dict(d)
        for k in ("lookbacks", "markets"):
            if k in d:
                d[k] = tuple(d[k])
        return Params(**d)


@dataclass
class Features:
    score: np.ndarray        # 大きいほど強い。対象外は NaN
    rank: np.ndarray         # 1 が最上位。対象外は大きな数
    eligible: np.ndarray     # 新規に買ってよい銘柄
    exit_trend: np.ndarray   # 自分の長期線を割っている
    risk_on: np.ndarray      # その銘柄の市場が買ってよい地合いか
    rebalance_day: np.ndarray  # その日が週次 (月次) の判定日か
    sma_entry: np.ndarray
    size_mult: np.ndarray    # 買う金額の倍率 (値動きの大きい銘柄は 1 未満)
    vol: np.ndarray          # 年率の値動き
    ret: dict = field(default_factory=dict)


NO_RANK = 10**6


def _sma(a: np.ndarray, w: int) -> np.ndarray:
    return pd.DataFrame(a).rolling(w, min_periods=w).mean().to_numpy()


def _ret(close: np.ndarray, lb: int, skip: int = 0) -> np.ndarray:
    out = np.full_like(close, np.nan)
    if lb + skip < len(close):
        end = close[lb:] if skip == 0 else close[lb:-skip]
        out[lb + skip:] = end / close[:len(end)] - 1.0
    return out


def _clenow(close: np.ndarray, w: int = 90) -> np.ndarray:
    """log 価格の直線回帰の傾き (年率) × 決定係数。なめらかに上がっている銘柄ほど大きい。"""
    y = pd.DataFrame(np.log(close))
    x = pd.DataFrame(np.broadcast_to(np.arange(len(close), dtype=float)[:, None], close.shape))
    mx, my = x.rolling(w).mean(), y.rolling(w).mean()
    cov = (x * y).rolling(w).mean() - mx * my
    vx = (x * x).rolling(w).mean() - mx * mx
    vy = (y * y).rolling(w).mean() - my * my
    slope = cov / vx
    r2 = (cov * cov) / (vx * vy)
    return ((np.exp(slope * 252) - 1.0) * r2).to_numpy()


def compute_features(panel: Panel, p: Params) -> Features:
    close = panel.close
    n, m = close.shape
    rets = {lb: _ret(close, lb, p.skip) for lb in p.lookbacks}
    blend = np.mean([rets[lb] for lb in p.lookbacks], axis=0)
    if p.score == "blend":
        score = blend
    elif p.score == "riskadj":
        daily = pd.DataFrame(close).pct_change(fill_method=None)
        vol = daily.rolling(126, min_periods=100).std().to_numpy() * np.sqrt(252)
        score = blend / vol
    elif p.score == "clenow":
        score = _clenow(close)
    else:
        raise ValueError(f"score が不明: {p.score}")

    age = np.cumsum(~np.isnan(close), axis=0)
    liquid = np.zeros((n, m), dtype=bool)
    for cur in ("JPY", "USD"):
        cols = [j for j, t in enumerate(panel.tickers) if currency(t) == cur]
        if cols:
            dv = pd.DataFrame(panel.dollar_vol[:, cols])
            limit = np.ceil(p.liquid_frac * dv.notna().sum(axis=1).to_numpy())
            liquid[:, cols] = (-dv).rank(axis=1, method="first").to_numpy() <= limit[:, None]
    sma_entry = _sma(close, p.entry_sma)
    in_market = np.array([currency(t) in p.markets for t in panel.tickers])
    base = (age >= p.min_history) & liquid & ~np.isnan(score) & in_market[None, :]
    score = np.where(base, score, np.nan)
    rank = (-pd.DataFrame(score)).rank(axis=1, method="first").fillna(NO_RANK).to_numpy()

    risk_on = np.ones((n, m), dtype=bool)
    if p.regime_sma:
        for cur, idx in panel.index_close.items():
            on = idx > _sma(idx[:, None], p.regime_sma)[:, 0]
            cols = [j for j, t in enumerate(panel.tickers) if currency(t) == cur]
            risk_on[:, cols] = on[:, None]

    above = close > sma_entry
    if p.breadth_min:
        for cur in panel.index_close:
            cols = [j for j, t in enumerate(panel.tickers) if currency(t) == cur]
            known = (~np.isnan(sma_entry[:, cols])).sum(axis=1)
            breadth = np.where(known > 0, above[:, cols].sum(axis=1) / np.maximum(known, 1), 0.0)
            risk_on[:, cols] &= (breadth >= p.breadth_min)[:, None]

    eligible = base & above & risk_on
    if p.near_high:
        high = pd.DataFrame(close).rolling(p.high_window, min_periods=p.high_window // 2).max().to_numpy()
        eligible &= close >= high * (1.0 - p.near_high)
    daily = pd.DataFrame(close).pct_change(fill_method=None)
    vol = daily.rolling(p.vol_window, min_periods=p.vol_window // 2).std().to_numpy() * np.sqrt(260)
    size_mult = np.ones((n, m))
    if p.vol_cap:
        size_mult = np.clip(p.vol_cap / np.where(vol > 0, vol, np.nan), None, 1.0)
        size_mult = np.where(np.isnan(size_mult), 1.0, size_mult)
    exit_trend = np.zeros((n, m), dtype=bool)
    if p.exit_sma:
        exit_trend = close < _sma(close, p.exit_sma)

    reb = np.zeros(n, dtype=bool)
    if p.rebalance == "W":
        week = week_number(panel.dates)
        reb[:-1] = (week[1:] != week[:-1]) & (week[:-1] % p.rebalance_every == p.rebalance_offset)
    else:
        month = panel.dates.month.to_numpy()
        reb[:-1] = month[1:] != month[:-1]
    # 最終行は「その週 (月) の最後の営業日かどうか」がまだ分からない。日次の運用側で明示的に指定する
    return Features(score, rank, eligible, exit_trend, risk_on, reb, sma_entry, size_mult, vol, rets)


def week_number(dates: pd.DatetimeIndex) -> np.ndarray:
    """月曜始まりの週の通し番号 (1970-01-05 の週が 0)。"""
    return ((dates - pd.Timestamp("1970-01-05")).days // 7).to_numpy()


@dataclass
class Holding:
    entry_i: int          # 買った日のパネル上の位置 (最高値を数え始める日)
    shares: float = 0.0


@dataclass
class Decision:
    sells: list           # [(列, 理由)]
    buys: list            # [列] 優先順
    trims: list = field(default_factory=list)   # [列] 目標の割合まで減らす
    adds: list = field(default_factory=list)    # [列] 目標の割合まで買い増す


def decide(panel: Panel, feat: Features, i: int, held: dict[int, Holding], p: Params,
           rebalance: bool | None = None, weights: dict[int, float] | None = None,
           affordable=None) -> Decision:
    """i 日目の終値で判定する。held は {列: Holding}。affordable(列) が False の銘柄は買い候補から外す。"""
    rebalance = bool(feat.rebalance_day[i]) if rebalance is None else rebalance
    sells, trims, adds = [], [], []
    for j, h in held.items():
        px = panel.close[i, j]
        if p.trail_stop:
            high = np.nanmax(panel.close[h.entry_i:i + 1, j])
            if px < high * (1.0 - p.trail_stop):
                sells.append((j, f"trail_stop: 高値 {high:.2f} から {px / high - 1:+.1%}"))
                continue
        if not rebalance:
            continue
        if p.regime_exit and not feat.risk_on[i, j]:
            sells.append((j, "regime: 市場の指数が長期線の下"))
        elif feat.exit_trend[i, j]:
            sells.append((j, f"trend: 終値が {p.exit_sma}日線の下"))
        elif feat.rank[i, j] > p.hold_rank:
            r = int(feat.rank[i, j])
            what = f"順位が {r} 位に落ちた" if r < NO_RANK else "順位づけの対象外になった"
            sells.append((j, f"rank: {what} ({p.hold_rank} 位以内なら持ち続ける)"))
        elif p.trim_above and weights and weights.get(j, 0.0) > p.trim_above:
            trims.append(j)
        elif (p.topup_below and weights and feat.eligible[i, j]
              and weights.get(j, 0.0) < p.topup_below * p.target_weight() * feat.size_mult[i, j]):
            adds.append(j)
    if not rebalance:
        return Decision(sells, [], [])

    selling = {j for j, _ in sells}
    keep = [j for j in held if j not in selling]
    slots = p.top_n - len(keep)
    buys = []
    if slots > 0:
        count = {}
        for j in keep:
            g = panel.group_of.get(panel.tickers[j])
            count[g] = count.get(g, 0) + 1
        order = np.argsort(feat.rank[i])
        for j in order:
            if len(buys) >= slots or feat.rank[i, j] >= NO_RANK:
                break
            # 買ってすぐ売りになる順位 (hold_rank より下) の銘柄は買わない。いま売ると決めた銘柄をその場で買い戻すこともしない
            if j in held or not feat.eligible[i, j] or feat.rank[i, j] > p.hold_rank:
                continue
            g = panel.group_of.get(panel.tickers[j])
            if count.get(g, 0) >= p.max_per_group:
                continue
            if affordable is not None and not affordable(int(j)):
                continue
            buys.append(int(j))
            count[g] = count.get(g, 0) + 1
    return Decision(sells, buys, trims, adds)
