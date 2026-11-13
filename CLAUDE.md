# dejong — Claude による株の半自動売買 (楽天証券)

- 判断は Claude がスキルで行い、計算・リスクチェック・記録は `trader/` パッケージが行う。発注は人が楽天証券で行う
- 日次: `.claude/skills/daily-trade/` / 週次: `.claude/skills/weekly-research/` / 約定やスクショの反映: `.claude/skills/sync-account/` / ルールの検証と改善: `.claude/skills/improve-rules/`
- 売買ルールは3層: 絶対の上限 (`settings.yaml` の risk) > 機械ルール (`rules.yaml`、バックテスト可能) > 裁量 (`strategy.md`)
- 口座状態は `state/` (JSON/CSV) にあり、git の履歴がそのまま取引記録になる。手で編集しない
- 金額は円換算で扱う (米国株の取得単価だけはドル建て)
- 人間が管理するファイル: `config/strategy.md`, `config/settings.yaml`, `config/rules.yaml`, `config/universe.yaml`
- リポジトリ直下に `STOP` ファイルを置くと全注文が止まる (緊急停止)
- `surge/` は別の売買システム (集中モメンタム、米国株5銘柄)。運用は `.claude/skills/surge-trade/`、説明は `surge/README.md`。
  口座の記録 (`state/`) は共有で surge は読むだけ。ルールの数字は `surge/config.yaml` (検証とセットなので、人の了承なしに変えない)。
  日次ルーチンと surge の注文案を同じ日に両方出さない (どちらで運用するかは人が決める)
- テスト: `python -m pytest -q` (ネットワーク不要)
