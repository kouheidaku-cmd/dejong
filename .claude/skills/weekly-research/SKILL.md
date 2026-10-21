---
name: weekly-research
description: 週1回の注目株リサーチ。母集団のスクリーニング、候補銘柄と保有銘柄のニュース・決算調査、ウォッチリスト更新、1週間の売買の振り返りを行う。「週次リサーチ」「注目株を調べて」「weekly-research」と言われたら使う。
---

# 週次リサーチ

週末 (土曜か日曜) に実行する。ここで作るウォッチリストと投資仮説が、翌週の日次売買の前提になる。

## ルール
- 新規買いが許されるのは `config/watchlist.yaml` の銘柄だけ (コードで強制)。ここに載せる = 買ってよい候補
- ウォッチリストは日米合わせて最大12銘柄。仮説を書けない銘柄は載せない
- 母集団は日本株 (`.T`) と米国株の両方。口座が小さいので、1株単位で買える金額かも意識する
- `state/` と `config/strategy.md`、`config/settings.yaml` は編集しない (変更はレポートで提案)
- ニュースや数字は出典を確認できたものだけ書く。推測は推測と明記する

## 手順

1. **準備**
   ```bash
   git pull --ff-only
   pip install -q -r requirements.txt
   ```

2. **スクリーニング**
   ```bash
   python -m trader screen --top 15
   ```
   `config/universe.yaml` の銘柄をモメンタム中心のスコアで並べたもの (全件は `reports/weekly/<年-W週>/screen.json`)。
   あくまで候補出し。`trend_ok` やRSIの過熱感も見る。

3. **調査** — 上位候補・現ウォッチリスト・保有銘柄 (`python -m trader status`) について WebSearch で調べる:
   - 直近のニュース、決算内容、会社のガイダンス修正
   - **次回決算発表日** (直前の新規買いを避けるため必ず確認)
   - セクター・市場全体の地合い (日経平均・TOPIX、S&P500・NASDAQ、ドル円、金利など)

4. **1週間の振り返り** — `state/trades.csv` と `state/equity.csv`、今週の日次レポートを読み:
   - 注文案のうち実行されたもの / されなかったもの (`proposal_unconfirmed`) の確認
   - 週間の損益と主な要因
   - うまくいった判断 / まずかった判断と、その理由
   - 損失の出たトレードが、機械ルールどおりの結果か / 裁量 (見送り・前倒し売り) の結果かを分ける
   - 機械ルールで損失が目立つ (損切りが続く、同じパターンで負けている) なら、improve-rules スキルで検証することを提案する
   - `strategy.md` や `settings.yaml` に変更を提案するならその内容と根拠

   - フォワードテスト (ルールを決めた後の実データでの記録) を更新して読む:
     ```bash
     python -m trader forward
     ```
     `config/forward.yaml` の各ルールと指数 50/50 の、記録開始日からの成績が出る (`reports/forward/`)。
     レポートに表で載せる。開始から3か月未満のうちは、成績の差で結論を出さない

5. **ウォッチリスト更新** — `config/watchlist.yaml` を書き換える:
   ```yaml
   updated: 2026-10-25
   tickers:
     - ticker: 8035.T
       name: 東京エレクトロン
       thesis: 生成AI向け投資で前工程装置の受注が回復。上方修正の余地 (根拠: ...)
       risks: 対中輸出規制の強化、11/10 決算
       next_earnings: 2026-11-10
       added: 2026-10-25
   ```
   - 外す銘柄・加える銘柄はそれぞれ理由をレポートに書く
   - 保有中の銘柄を外してもよい (売りはいつでも可能、買い増しができなくなるだけ)

6. **週次レポート** — `reports/weekly/<年-W週>/report.md` に、地合い・振り返り・ウォッチリストの変更と各銘柄の仮説・
   来週の注目イベント (決算・指標発表) をまとめる。

7. **口座の照合をお願いする** — 最後の返答で、楽天の「保有銘柄」と「総合サマリー」のスクショを送ってもらうよう頼む
   (受け取ったら sync-account スキルで記録を合わせる)

8. **記録を残す**
   ```bash
   git add config/watchlist.yaml reports rules
   git commit -m "weekly: <年-W週>"
   git push
   ```
