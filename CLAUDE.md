# dejong — Claude による株の半自動売買 (楽天証券)

- 判断は Claude がスキルで行い、計算・リスクチェック・記録は `trader/` パッケージが行う。発注は人が楽天証券で行う
- 日次: `.claude/skills/daily-trade/` / 週次: `.claude/skills/weekly-research/` / 約定やスクショの反映: `.claude/skills/sync-account/` / ルールの検証と改善: `.claude/skills/improve-rules/`
- 売買ルールは3層: 絶対の上限 (`settings.yaml` の risk) > 機械ルール (`rules.yaml`、バックテスト可能) > 裁量 (`strategy.md`)
- 口座状態は `state/` (JSON/CSV) にあり、git の履歴がそのまま取引記録になる。手で編集しない
- 金額は円換算で扱う (米国株の取得単価だけはドル建て)
- 人間が管理するファイル: `config/strategy.md`, `config/settings.yaml`, `config/rules.yaml`, `config/universe.yaml`
- リポジトリ直下に `STOP` ファイルを置くと全注文が止まる (緊急停止)
- **2026-11-14 から、実際の口座は `surge/` で運用している (人の決定)。** surge は別の売買システム (集中モメンタム、米国株5銘柄) で、
  運用は `.claude/skills/surge-trade/` (週末に入れ替え、平日は高値から -25% の確認)、説明は `surge/README.md`。
  daily-trade・weekly-research の注文案は出さない。v3 と候補の成績はフォワードテストで記録を続け、2027年1月下旬に見直す。
  口座の記録 (`state/`) は共有で surge は読むだけ (約定の記録は今までどおり sync-account)。ルールの数字は `surge/config.yaml` (検証とセットなので、人の了承なしに変えない)。
  `config/strategy.md` は v3 の説明のまま (人が管理するファイルなので書き換えていない)
- テスト: `python -m pytest -q` (ネットワーク不要)
