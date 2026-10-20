"""注文のリスクチェック。Claude が出した注文はすべてここを通ってから発注 (または注文案に) される。

ここで拒否された注文は出さない。上限値は config/settings.yaml の risk セクション。
金額の比較はすべて基準通貨 (円) に換算して行う。
"""
from __future__ import annotations

from .config import currency, lot_size
from .portfolio import Portfolio


def check_orders(
    orders: list[dict],
    pf: Portfolio,
    prices: dict[str, float],
    fx: dict[str, float],
    watchlist: set[str],
    settings: dict,
    stopped: bool = False,
) -> list[tuple[dict, str | None]]:
    """各注文に対して (order, 拒否理由 or None) を返す。前の注文の影響を見込んで順に判定する。"""
    r = settings["risk"]
    equity = pf.equity(prices, fx)
    peak = max(pf.peak_equity, equity)
    drawdown = 1 - equity / peak if peak > 0 else 0.0

    # 未約定注文を反映した見込みの現金・保有。
    # held は買い注文も含む見込み保有 (銘柄比率の判定用)、sellable は約定済みで売れる数。
    # 売却代金はその日のうちに買付に使える (楽天の仕様) ので、売りは現金に足す。
    cash = pf.cash
    held = {t: p["qty"] for t, p in pf.positions.items()}
    sellable = dict(held)

    def apply(o):
        nonlocal cash
        t, q = o["ticker"], o["qty"]
        amount = q * (_est_price(o, prices, r) or 0) * fx.get(currency(t, settings), 0)
        if o["side"] == "buy":
            cash -= amount
            held[t] = held.get(t, 0) + q
        else:
            cash += amount
            held[t] = held.get(t, 0) - q
            sellable[t] = sellable.get(t, 0) - q

    for o in pf.pending:
        apply(o)

    # 同じ日の売りの代金を買いに回せるよう、売りを先に判定する
    results, accepted = {}, 0
    for i, o in sorted(enumerate(orders), key=lambda x: x[1].get("side") != "sell"):
        reason = _check_one(o, prices, fx, watchlist, settings, held, sellable, cash, equity, drawdown, stopped, accepted)
        if reason is None:
            accepted += 1
            apply(o)
        results[i] = (o, reason)
    return [results[i] for i in range(len(orders))]


def _check_one(o, prices, fx, watchlist, settings, held, sellable, cash, equity, drawdown, stopped, accepted):
    r = settings["risk"]
    ticker, side, qty = o.get("ticker"), o.get("side"), o.get("qty")
    if stopped:
        return "STOP ファイルがあるため全注文停止中"
    if not ticker:
        return "ticker が空"
    if side not in ("buy", "sell"):
        return f"side が不正: {side}"
    if not isinstance(qty, int) or isinstance(qty, bool) or qty <= 0:
        return f"qty は正の整数: {qty}"
    lot = lot_size(ticker, settings)
    if qty % lot:
        return f"qty は売買単位 {lot} の倍数にする"
    if o.get("type", "market") not in ("market", "limit"):
        return f"type が不正: {o.get('type')}"
    if not str(o.get("rationale", "")).strip():
        return "rationale (理由) が空"
    if accepted >= r["max_orders_per_day"]:
        return f"1日の注文数上限 {r['max_orders_per_day']} を超過"
    last = prices.get(ticker)
    if last is None:
        return "価格データがない"
    rate = fx.get(currency(ticker, settings))
    if rate is None:
        return "為替レートがない"
    if o.get("type") == "limit":
        limit = o.get("limit_price")
        if not isinstance(limit, (int, float)) or limit <= 0:
            return "指値注文には limit_price が必要"
        if abs(limit / last - 1) > r["max_limit_deviation"]:
            return f"指値が前日終値から {r['max_limit_deviation']:.0%} 以上離れている"

    if side == "sell":
        if qty > sellable.get(ticker, 0):
            return f"売却可能数 {sellable.get(ticker, 0)} を超える売り (空売り禁止・未約定の買いは売れない)"
        return None

    if ticker not in watchlist:
        return "ウォッチリスト外の銘柄は新規買い不可"
    if drawdown >= r["max_drawdown_pct"]:
        return f"ドローダウン {drawdown:.1%} が上限 {r['max_drawdown_pct']:.0%} に達しているため新規買い停止"
    cost = qty * _est_price(o, prices, r) * rate
    position_after = held.get(ticker, 0) * last * rate + cost
    if position_after > r["max_position_pct"] * equity:
        return f"買付後の銘柄比率 {position_after / equity:.1%} が上限 {r['max_position_pct']:.0%} を超える"
    if cash - cost < r["min_cash_pct"] * equity:
        return f"買付後の現金が下限 {r['min_cash_pct']:.0%} を下回る (見込み現金 {cash:,.0f} 円, 必要 {cost:,.0f} 円)"
    return None


def _est_price(o: dict, prices: dict[str, float], r: dict) -> float | None:
    """現地通貨での見積り価格。成行は不利な方向に price_buffer だけずらす。"""
    if o.get("type") == "limit" and o.get("limit_price"):
        return float(o["limit_price"])
    last = prices.get(o["ticker"])
    if last is None:
        return None
    return last * (1 + r["price_buffer"] if o["side"] == "buy" else 1 - r["price_buffer"])
