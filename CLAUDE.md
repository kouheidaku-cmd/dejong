# dejong — Claude による株の自動売買 (ペーパートレード)

- 判断は Claude がスキルで行い、計算・リスクチェック・発注・記録は `trader/` パッケージが行う
- 日次: `.claude/skills/daily-trade/` / 週次: `.claude/skills/weekly-research/`
- 口座状態は `state/` (JSON/CSV) にあり、git の履歴がそのまま取引記録になる。手で編集しない
- 人間が管理するファイル: `config/strategy.md`, `config/settings.yaml`, `config/universe.yaml`
- リポジトリ直下に `STOP` ファイルを置くと全注文が止まる (緊急停止)
- テスト: `python -m pytest -q` (ネットワーク不要)
