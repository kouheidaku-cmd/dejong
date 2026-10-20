# dejong

Claude Code のスキルで株を売買する仕組み。**今はペーパートレード (仮想売買) のみ**。

## 構成

```
週1回 (週末)  weekly-research スキル
  screen (モメンタムで候補出し) → ニュース・決算を調査 → watchlist.yaml 更新 + 週次レポート

毎営業日 (大引け後)  daily-trade スキル
  prep    前日注文を寄付きで約定 → 口座評価 → 判断材料 snapshot.json
  Claude  strategy.md と週次の仮説に沿って orders.json を書く
  submit  リスクチェック (コード) を通った注文だけ発注 → 翌営業日の寄付きで約定
  Claude  日次レポートを書いて commit / push
```

役割分担がポイント:

| 担当 | 内容 |
|---|---|
| Claude (スキル) | 銘柄調査、投資仮説、売買判断、レポート |
| `trader/` (コード) | データ取得、指標計算、**リスク上限の強制**、約定処理、記録 |
| 人間 | `strategy.md` (方針)、`settings.yaml` (上限値)、緊急停止 (`STOP`) |

LLM の判断は毎回ぶれるので、「1銘柄25%まで」「ドローダウン15%で新規買い停止」「ウォッチリスト外は買えない」
「空売り不可」などの上限は `trader/risk.py` でコードとして強制しています。

## ディレクトリ

```
config/
  settings.yaml   資金・リスク上限・約定条件
  strategy.md     売買方針 (人間が書く)
  watchlist.yaml  注目株 (週次で更新、新規買いはここの銘柄のみ)
  universe.yaml   スクリーニング母集団
state/            口座状態 (portfolio.json, trades.csv, equity.csv)
reports/daily/    日次の snapshot / orders / 結果 / レポート
reports/weekly/   週次の screen / レポート
trader/           Python パッケージ
.claude/skills/   daily-trade, weekly-research
```

## 使い方

```bash
pip install -r requirements.txt
python -m trader init            # 口座初期化 (settings.yaml の initial_cash)
python -m trader screen          # 週次スクリーニング
python -m trader prep            # 日次の準備
python -m trader submit reports/daily/2026-10-20/orders.json
python -m trader status
python -m pytest -q
```

Claude Code では `/weekly-research` → `/daily-trade` の順に実行します。

## 定期実行

どちらかで動かします。状態 (`state/`) はリポジトリにしか残らないので、**毎回同じブランチ (main) に push される**ことが前提です。

- **Claude Code のルーチン (スケジュール実行)**: 日次は平日 16:30 JST に `/daily-trade`、週次は土曜に `/weekly-research`
- **GitHub Actions + claude-code-action**: cron で起動して main に直接コミット。`ANTHROPIC_API_KEY` を Secrets に登録

市場データは Yahoo Finance (yfinance) から取るので、実行環境から `query1.finance.yahoo.com` などに通信できる必要があります。

## 実口座につなぐ前に

1. 最低 1〜3 か月はペーパートレードで動かし、レポートと損益を確認する
2. `trader/broker/` に `PaperBroker` と同じ `submit` / `settle` を持つ実ブローカーを追加する
3. 小さい金額から始め、`settings.yaml` の上限を厳しめにする
