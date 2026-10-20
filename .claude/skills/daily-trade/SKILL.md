---
name: daily-trade
description: 1日1回の売買ルーチン。前日注文の約定処理、保有・ウォッチリスト銘柄の確認、売買判断、リスクチェック付き発注、日次レポート作成とコミットまでを行う。「今日の売買」「日次実行」「daily-trade」と言われたら使う。
---

# 日次売買ルーチン

東証の大引け後 (16時以降 JST) に1日1回実行する想定。当日の終値で判断し、注文は翌営業日の寄付きで約定する。

## ルール (必ず守る)
- `state/` 以下を手で編集しない。口座状態は `python -m trader ...` だけが書き換える
- 発注は必ず `python -m trader submit` 経由。リスクチェックで拒否された注文を回避・再投入しない
- `config/strategy.md` と `config/settings.yaml` は人間の管理。変更したければレポートで提案する
- 判断に迷ったら注文しない。「売買なし」は正当な結論

## 手順

1. **準備**
   ```bash
   git pull --ff-only
   pip install -q -r requirements.txt
   ```
   `state/portfolio.json` がなければ `python -m trader init` を実行する。

2. **約定処理とスナップショット**
   ```bash
   python -m trader prep
   ```
   出力 (= `reports/daily/<日付>/snapshot.json`) を読む。含まれるもの: 口座 (現金・総資産・ドローダウン)、
   保有銘柄と指標、前日注文の約定結果、ウォッチリスト銘柄の指標、リスク上限。

3. **今日やるか判定**
   - `stopped: true` (リポジトリ直下に `STOP` ファイル) → 注文せず、レポートだけ書いて終了
   - `fresh: false` (今日の日足がない = 休場日かデータ遅延) → 注文せず、その旨をレポートに書いて終了

4. **判断材料を集める**
   - `config/strategy.md` (売買方針) を読む
   - 最新の週次レポート `reports/weekly/*/report.md` を読み、各銘柄の投資仮説を確認
   - 保有銘柄で値動きが大きいもの (|ret_1d| > 4% や volume_ratio > 2) は WebSearch でニュースを確認
   - 直近の `reports/daily/*/report.md` を2〜3日分読み、判断に一貫性を持たせる

5. **注文を決める** — `strategy.md` に沿って、保有銘柄の売り → 新規・買い増しの順に検討する。
   `reports/daily/<日付>/orders.json` に書く (注文なしでも空配列で書く):
   ```json
   {
     "date": "2026-10-20",
     "orders": [
       {"ticker": "7203.T", "side": "buy", "qty": 100, "type": "market",
        "rationale": "SMA50上で押し目、週次仮説(〇〇)に変化なし。目標比率の半分でエントリー"}
     ],
     "notes": "全体の判断メモ"
   }
   ```
   - `qty` は `lot_size` (東証は100株) の倍数
   - `type` は `market` か `limit` (`limit_price` 必須、前日終値±10%以内)
   - `rationale` は必須。後で振り返れるように具体的に

6. **発注**
   ```bash
   python -m trader submit reports/daily/<日付>/orders.json
   ```
   `rejected` があれば理由を確認する。拒否はリスク管理が働いた結果なので、数量を減らすなど
   方針に沿った修正が明らかな場合に限り、修正版を `--force` で再提出してよい (1回まで)。

7. **日次レポート** — `reports/daily/<日付>/report.md` に書く:
   - 口座サマリー (総資産、前日比、ドローダウン、現金比率)
   - 約定した注文と損益
   - 今日出した注文と理由 / 拒否された注文と理由
   - 保有銘柄ごとの一言コメントと、明日以降に注意する点

8. **記録を残す**
   ```bash
   git add state reports
   git commit -m "daily: <日付>"
   git push
   ```
   状態はリポジトリにしか残らないので、push まで必ず行う。
