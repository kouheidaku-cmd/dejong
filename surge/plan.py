"""今の口座と最新の終値から、注文案を作る。

判定は strategy.decide (検証と同じ関数)。ここでは、その結果を「何を何株」に直し、
絶対の上限 (1銘柄の比率・現金の下限・STOP) を当てはめる。発注は人が楽天証券で行う。
"""
from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd

from .account import Account
from .backtest import size_order
from .data import Panel, currency
from .strategy import NO_RANK, Holding, Params, compute_features, decide


def _last_bar(panel: Panel, cur: str) -> date:
    cols = [j for j, t in enumerate(panel.tickers) if currency(t) == cur]
    has = (~np.isnan(panel.open[:, cols])).any(axis=1)
    return panel.dates[np.flatnonzero(has)[-1]].date()


def expected_sessions(now: pd.Timestamp) -> dict[str, date]:
    """日本時間の now の時点で、終値が出ているはずの最後の営業日 (祝日は考えない)。"""
    def weekday_on_or_before(d):
        while d.weekday() >= 5:
            d -= pd.Timedelta(days=1)
        return d

    now = now.tz_convert("Asia/Tokyo")
    day = now.normalize()
    jp = day if (now.hour, now.minute) >= (15, 50) else day - pd.Timedelta(days=1)
    us = day - pd.Timedelta(days=1 if (now.hour, now.minute) >= (6, 30) else 2)   # 米国の終値は日本の翌朝
    return {"JPY": weekday_on_or_before(jp).date(), "USD": weekday_on_or_before(us).date()}


def make_plan(panel: Panel, p: Params, acct: Account, exec_cfg: dict, rebalance: bool,
              exclude: tuple[str, ...] = (), today: date | None = None, expected: dict | None = None) -> dict:
    feat = compute_features(panel, p)
    i = len(panel.dates) - 1
    today = today or date.today()
    buffer = float(exec_cfg.get("price_buffer", 0.03))
    mult = float(exec_cfg.get("max_weight_mult", 1.3))
    col = {t: j for j, t in enumerate(panel.tickers)}
    warnings = []

    def px_jpy(j):
        return float(panel.raw_close[i, j] * panel.fx[i, j])

    # --- 口座の評価
    held, values, outside = {}, {}, []
    for t, pos in acct.positions.items():
        if t not in col:
            outside.append(t)
            continue
        j = col[t]
        opened = pd.Timestamp(pos.get("opened") or panel.dates[i])
        held[j] = Holding(entry_i=min(int(panel.dates.searchsorted(opened)), i), shares=float(pos["qty"]))
        values[j] = pos["qty"] * px_jpy(j)
    usd = next((float(panel.fx[i, j]) for j, t in enumerate(panel.tickers) if currency(t) == "USD"), 1.0)
    outside_value = {t: acct.positions[t]["qty"] * acct.positions[t]["avg_cost"] * (usd if currency(t) == "USD" else 1.0)
                     for t in outside}
    if outside:
        warnings.append(f"母集団にない保有があります (取得額で評価し、売買の対象にしません): {', '.join(outside)}")
    equity = acct.cash + sum(values.values()) + sum(outside_value.values())
    weights = {j: v / equity for j, v in values.items()}
    cash_floor = max(p.cash_buffer, acct.min_cash_pct) * equity
    target = p.target_weight() * equity
    cap = min(mult * target, acct.max_position_pct * equity)

    # --- 判定
    skipped = []

    def affordable(j):
        t = panel.tickers[j]
        if t in exclude:
            skipped.append({"ticker": t, "why": "裁量で見送り (--exclude)"})
            return False
        if px_jpy(j) * (1 + buffer) > cap:
            skipped.append({"ticker": t, "why": f"1株 {px_jpy(j):,.0f}円が1銘柄の上限 {cap:,.0f}円を超える"})
            return False
        return True

    d = decide(panel, feat, i, held, p, rebalance=rebalance, weights=weights, affordable=affordable)

    # --- 注文
    orders = []
    reasons = dict(d.sells)
    proceeds = jp_proceeds = 0.0
    for j, reason in sorted(d.sells, key=lambda x: currency(panel.tickers[x[0]]) != "JPY"):   # 先に開く日本株から
        qty = int(held[j].shares)
        amt = qty * px_jpy(j)
        proceeds += amt * (1 - buffer)
        if currency(panel.tickers[j]) == "JPY":
            jp_proceeds += amt * (1 - buffer)
        orders.append(_order(panel, i, j, "sell", qty, amt, reason))
    for j in d.trims:
        qty = int(np.floor((values[j] - target) / px_jpy(j)))
        if qty > 0:
            amt = qty * px_jpy(j)
            proceeds += amt * (1 - buffer)
            reasons[j] = f"trim: 比率 {weights[j]:.0%} が上限 {p.trim_above:.0%} を超えた"
            orders.append(_order(panel, i, j, "sell", qty, amt, reasons[j]))
    avail = acct.cash + proceeds - cash_floor
    # 最初の寄付きで使える現金: 日本株の買いは今の現金だけ、米国株の買いはその朝の日本株の売却代金も使える
    first = {"JPY": acct.cash - cash_floor, "USD": acct.cash + jp_proceeds - cash_floor}
    for j in d.buys + d.adds:
        t = panel.tickers[j]
        est = px_jpy(j) * (1 + buffer)
        full = target * float(feat.size_mult[i, j])
        want = full - values.get(j, 0.0)          # 買い増しなら、目標との差だけ
        if avail < 0.5 * want:
            skipped.append({"ticker": t, "why": f"現金が足りない (使える見込み {max(avail, 0):,.0f}円、目安 {want:,.0f}円)"})
            continue
        qty = int(size_order(want, est, cap - values.get(j, 0.0), avail, integer=True))
        if qty <= 0:
            if j not in held:
                skipped.append({"ticker": t, "why": "現金が足りず1株も買えない"})
            continue
        amt = qty * px_jpy(j)
        needs_proceeds = qty * est > first[currency(t)]
        first = {k: v - qty * est for k, v in first.items()}
        avail -= qty * est
        reason = (f"{'topup: 目標の金額まで買い増し。' if j in held else 'entry: '}順位 {int(feat.rank[i, j])} 位 "
                  f"(スコア {feat.score[i, j]:+.2f})、3か月 {feat.ret[p.lookbacks[0]][i, j]:+.0%}、200日線より上、高値圏")
        if j in held:
            reasons[j] = "topup: 買い増し"
        o = _order(panel, i, j, "buy", qty, amt, reason)
        o["after_sells"] = bool(needs_proceeds)
        orders.append(o)
    if acct.stopped and orders:
        warnings.append("STOP ファイルがあるため、注文案をすべて取り消しました")
        orders = []

    # --- 表示用
    holdings = []
    for j, h in held.items():
        high = float(np.nanmax(panel.close[h.entry_i:i + 1, j]))
        adj = float(panel.close[i, j])
        r = int(feat.rank[i, j])
        holdings.append({
            "ticker": panel.tickers[j], "qty": int(h.shares), "price": float(panel.raw_close[i, j]),
            "value": values[j], "weight": weights[j], "rank": r if r < NO_RANK else None,
            "above_sma": bool(not feat.exit_trend[i, j]), "from_high": adj / high - 1.0,
            "to_stop": (high * (1 - p.trail_stop) / adj - 1.0) if p.trail_stop else None,
            "action": "sell" if j in dict(d.sells) else "trim" if j in d.trims and j in reasons
                      else "add" if reasons.get(j, "").startswith("topup") else "keep",
            "reason": reasons.get(j, ""),
        })
    holdings.sort(key=lambda x: -x["value"])

    ranking = []
    for j in np.argsort(feat.rank[i])[:25]:
        if feat.rank[i, j] >= NO_RANK:
            break
        why = []
        if not feat.risk_on[i, j]:
            why.append("市場の指数が200日線の下")
        if not panel.close[i, j] > feat.sma_entry[i, j]:
            why.append("200日線の下")
        elif not feat.eligible[i, j] and feat.risk_on[i, j]:
            why.append("高値から離れすぎ")
        ranking.append({
            "rank": int(feat.rank[i, j]), "ticker": panel.tickers[j], "group": panel.group_of.get(panel.tickers[j]),
            "score": float(feat.score[i, j]), "ret_3m": float(feat.ret[p.lookbacks[0]][i, j]),
            "vol": float(feat.vol[i, j]), "price_jpy": px_jpy(j), "eligible": bool(feat.eligible[i, j]),
            "held": bool(j in held), "why_not": "、".join(why),
        })

    regime = {}
    for cur, idx in panel.index_close.items():
        sma = float(pd.Series(idx).rolling(p.regime_sma).mean().iloc[-1]) if p.regime_sma else None
        regime[cur] = {"index": float(idx[i]), "sma": sma, "risk_on": bool(sma is None or idx[i] > sma)}

    as_of = {cur: _last_bar(panel, cur) for cur in ("JPY", "USD")}
    for cur, d_ in as_of.items():
        want = (expected or {}).get(cur)
        if want and d_ < want and (cur in p.markets or any(currency(t) == cur for t in acct.positions)):
            warnings.append(f"{'日本株' if cur == 'JPY' else '米国株'}の終値が {d_} までしか取れていない "
                            f"({want} の分がまだ。祝日でなければ、時間をおいて取り直す)")
    last_real = {cur: panel.dates.get_loc(pd.Timestamp(d_)) for cur, d_ in as_of.items()}
    for j in sorted(set(held) | set(d.buys)):          # 保有と買い候補は、銘柄ごとにも最新の足があるか確かめる
        t = panel.tickers[j]
        if np.isnan(panel.open[last_real[currency(t)], j]):
            warnings.append(f"{t} の最新の足が取れていない (売買停止か、データの取得漏れ。この銘柄の判定は古い価格によるもの)")
    buys_total = sum(o["amount_jpy"] for o in orders if o["side"] == "buy")
    if acct.nisa_remaining is not None and buys_total > acct.nisa_remaining:
        warnings.append(f"買いの合計 {buys_total:,.0f}円が NISA 成長投資枠の残り (概算 {acct.nisa_remaining:,.0f}円) を超える")

    sells_total = sum(o["amount_jpy"] for o in orders if o["side"] == "sell")
    return {
        "date": today.isoformat(), "mode": "weekly" if rebalance else "daily",
        "as_of": {k: v.isoformat() for k, v in as_of.items()},
        "equity": equity, "cash": acct.cash, "other_assets": acct.other_assets,
        "target_per_position": target, "cap_per_position": cap,
        "regime": regime, "holdings": holdings, "orders": orders, "skipped": skipped, "ranking": ranking,
        "cash_after": acct.cash + sells_total - buys_total, "nisa_remaining": acct.nisa_remaining,
        "warnings": warnings, "excluded": list(exclude), "params": p.to_dict(),
    }


def _order(panel: Panel, i: int, j: int, side: str, qty: int, amount: float, reason: str) -> dict:
    t = panel.tickers[j]
    jp = currency(t) == "JPY"
    return {
        "ticker": t, "side": side, "qty": qty, "type": "market",
        "ref_price": float(panel.raw_close[i, j]), "currency": currency(t), "amount_jpy": float(amount),
        "when": "次の営業日の寄付き (日本株)" if jp else "次に開く米国市場の寄付き (米国株)",
        "reason": reason,
    }


# ---------------------------------------------------------------- レポート

def render(plan: dict) -> str:
    L = []
    mode = "週次 (入れ替えの判定あり)" if plan["mode"] == "weekly" else "日次 (高値からの下げだけ判定)"
    L.append(f"# surge 注文案 {plan['date']} — {mode}")
    L.append("")
    L.append(f"- データ: 日本株 {plan['as_of']['JPY']} / 米国株 {plan['as_of']['USD']} の終値")
    L.append(f"- 株 + 現金: {plan['equity']:,.0f}円 (現金 {plan['cash']:,.0f}円)。1銘柄の目安 {plan['target_per_position']:,.0f}円、上限 {plan['cap_per_position']:,.0f}円")
    for cur, name in (("USD", "S&P500"), ("JPY", "日経平均")):
        r = plan["regime"][cur]
        state = "200日線より上 → 買ってよい" if r["risk_on"] else "200日線より下 → この市場では新規に買わない"
        L.append(f"- {name}: {r['index']:,.0f} (200日線 {r['sma']:,.0f}) {state}" if r["sma"] else f"- {name}: {r['index']:,.0f}")
    for w in plan["warnings"]:
        L.append(f"- **注意**: {w}")
    L.append("")
    L.append("## 注文案")
    if not plan["orders"]:
        L.append("なし。")
    else:
        L.append("| 順 | 銘柄 | 売買 | 株数 | 参考価格 | 金額 (円) | いつ | 理由 |")
        L.append("|---|---|---|---|---|---|---|---|")
        for n, o in enumerate(plan["orders"], 1):
            side = "売り" if o["side"] == "sell" else "買い"
            when = o["when"] + (" ※買付余力が足りなければ、売却代金が入ってから" if o.get("after_sells") else "")
            unit = "円" if o["currency"] == "JPY" else "ドル"
            L.append(f"| {n} | {o['ticker']} | {side} | {o['qty']} | {o['ref_price']:,.2f}{unit} | {o['amount_jpy']:,.0f} | {when} | {o['reason']} |")
        L.append("")
        L.append(f"すべて約定した場合の現金の見込み: {plan['cash_after']:,.0f}円")
    if plan["skipped"]:
        L.append("")
        L.append("見送った候補: " + " / ".join(f"{s['ticker']} ({s['why']})" for s in plan["skipped"]))
    L.append("")
    L.append("## 保有")
    L.append("| 銘柄 | 株数 | 評価額 (円) | 比率 | 順位 | 200日線 | 高値から | 売りの線まで | 判定 |")
    L.append("|---|---|---|---|---|---|---|---|---|")
    for h in plan["holdings"]:
        act = {"keep": "持ち続ける", "sell": "売る", "trim": "減らす", "add": "買い増す"}[h["action"]]
        stop = f"{h['to_stop']:+.1%}" if h["to_stop"] is not None else "-"
        L.append(f"| {h['ticker']} | {h['qty']} | {h['value']:,.0f} | {h['weight']:.0%} | {h['rank'] or '対象外'} | "
                 f"{'上' if h['above_sma'] else '下'} | {h['from_high']:+.1%} | {stop} | {act} |")
    L.append("")
    L.append("## 順位 (上位)")
    L.append("| 順位 | 銘柄 | group | スコア | 3か月 | 1株 (円) | 買える | 備考 |")
    L.append("|---|---|---|---|---|---|---|---|")
    for r in plan["ranking"][:15]:
        note = "保有中" if r["held"] else r["why_not"]
        L.append(f"| {r['rank']} | {r['ticker']} | {r['group']} | {r['score']:+.2f} | {r['ret_3m']:+.0%} | {r['price_jpy']:,.0f} | "
                 f"{'○' if r['eligible'] else '×'} | {note} |")
    L.append("")
    return "\n".join(L)
