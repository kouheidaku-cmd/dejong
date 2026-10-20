# dejong

Claude Code のスキルで株を売買する仕組み。楽天証券の口座 (日本株・米国株) を**半自動**で運用する:
Claude が分析して注文案を出し、人が楽天証券のアプリで発注する。

## 流れ

```
週1回 (週末)          /weekly-research
  候補出し (日米60銘柄) → ニュース・決算を調査 → watchlist.yaml 更新 + 週次レポート

毎営業日 16:30 ごろ    /daily-trade
  prep    前回の注文案の確認 → 口座評価 (円換算) → 判断材料 snapshot.json
  Claude  strategy.md と週次の仮説に沿って orders.json を書く
  submit  リスクチェック (コード) を通った注文だけ「注文案」にする
  Claude  注文案の表と日次レポートを書いて commit / push

人                    楽天証券で発注 (米国株は当日夜、日本株は翌朝)
  → 約定照会や保有銘柄のスクショを送る → /sync-account で記録を更新
```

| 担当 | 内容 |
|---|---|
| Claude (スキル) | 銘柄調査、投資仮説、売買判断、レポート、スクショの読み取り |
| `trader/` (コード) | データ取得、指標計算、円換算、**リスク上限の強制**、記録 |
| 人 | 発注、`strategy.md` (方針)、`settings.yaml` (上限値)、緊急停止 (`STOP`) |

`trader/risk.py` がコードとして強制する主なルール: 1銘柄25%まで / 買付後に現金5%以上 /
ドローダウン15%で新規買い停止 / ウォッチリスト外は買えない / 空売り不可 / 1日5件まで / 理由必須 / 二重発注防止。

## ディレクトリ

```
config/
  settings.yaml   リスク上限・市場 (通貨・売買単位)・broker (manual / paper)
  strategy.md     売買方針 (人間が書く)
  watchlist.yaml  注目株 (週次で更新、新規買いはここの銘柄のみ)
  universe.yaml   スクリーニング母集団 (日本株30 + 米国株30)
state/
  portfolio.json  記録上の口座 (現金は円、米国株の取得単価はドル)
  trades.csv      注文案・約定・照合の履歴
  equity.csv      日次の総資産
  sync/           楽天の画面から作った照合データ
reports/daily/    日次の snapshot / orders / 結果 / レポート
reports/weekly/   週次の screen / レポート
trader/           Python パッケージ
.claude/skills/   daily-trade, weekly-research, sync-account
```

## コマンド

```bash
pip install -r requirements.txt
python -m trader prep                                  # 日次の準備
python -m trader submit reports/daily/<日付>/orders.json
python -m trader record MSFT sell 1 530.5 --fx 151.2   # 約定を1件記録
python -m trader sync state/sync/<日付>.yaml            # 保有一覧で上書き
python -m trader status
python -m trader screen                                 # 週次スクリーニング
python -m pytest -q
```

`settings.yaml` の `broker: paper` にすると、人の発注なしに翌営業日の寄付きで約定させる仮想売買になる。

## 定期実行

Claude Code のルーチン (スケジュール実行) で、日次は平日 16:30 JST に `/daily-trade`、週次は土曜に `/weekly-research` を実行する。
状態 (`state/`) はリポジトリにしか残らないので、**毎回同じブランチ (main) に push される**ことが前提。

市場データは Yahoo Finance (yfinance) から取るので、実行環境から `query1.finance.yahoo.com` などに通信できる必要がある。
