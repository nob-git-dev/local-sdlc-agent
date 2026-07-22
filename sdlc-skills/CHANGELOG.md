# Changelog

本リポジトリの変更履歴。[Keep a Changelog](https://keepachangelog.com/ja/1.1.0/) 形式、[Semantic Versioning](https://semver.org/lang/ja/) 準拠。

## [0.2.0] - 2026-06-01

判断の原則と「行動の憲法」を導入し、本番影響変更のセキュリティ検証を必須化したリリース。

### Added

#### Docs
- `docs/work-constitution.md` — Supervisor の行動正典テンプレート。「行動の憲法」全12条（検証した
  事実だけに従う／リスク勾配／権威ある定義元／全層を掃く／破壊半径／根治／継ぎ目／判断の外在化／
  ドメイン洞察／適用条件照合／能動的破壊試行／agent-native）と各条の本質。
- `docs/judgment-principles.md` — `CLAUDE.md` への手動追記用「判断と検証の原則」テンプレート（憲法の
  普遍要旨を 5 項目に圧縮。fork スキル・subagent への自動継承を想定）。

#### Agents
- `supervisor.md` §7「判断の原則」— 「速さ（ユーザーの危険信号は即検知）」と「慎重さ（自分が観測した
  異常は証拠で棄却してから報告）」の使い分け。
- `supervisor.md` §8「簡略実行の最小ゲート」— G1 本番特性の観測 / G2 本番影響変更の `/security` /
  G3 独立検証は、軽量モードでも省略不可。
- `supervisor.md` Step1 注記 — 危険信号の強制対応は簡略実行でも省略不可。

#### Skills
- `architect/` — 実行フロー [2]「現状の把握（Read before write）」に本番データ特性の観測と適用条件照合
  を追加。§6「Agent-native 設計観点」（構造化ログ・機械可読スキーマ・CLI/headless・`.env.example`・
  監査可能性）を追加。
- `security/` — §8「Adversarial モード（能動的破壊試行）」を追加。攻撃者視点での破壊・権限回避シナリオ
  生成（本番影響の大きい機能で実施）。
- `sdlc/` — 典型フローの「インフラ変更」に `/security` を必須化（注記付き）。品質ゲートの「→ デプロイ」に
  「本番影響変更は `/security` 検証済み」を追加。

### Changed
- `supervisor.md` `initialPrompt` — セッション開始時の状況確認を一般化（`git status` だけでなく、
  プロジェクトの管理方式に応じた環境の状態確認）。

### Fixed
- `spec/` — 「システム構成」例に残っていた具体的な内部コンポーネント名を、汎用例（認証 API サービス /
  ユーザー DB / Web フロントエンド等）に置換。0.1.0 の「プロジェクト固有識別子は匿名化済み」を完全化。

## [0.1.0] - 2026-04-12

初回公開リリース。

### Added

#### Skills (12)
- `sdlc/` — SDLC オーケストレーター
- `spec/` — 仕様定義
- `architect/` — クリーンアーキテクチャ + ADR 記録
- `tdd/` — Uncle Bob の三法則 + Red-Green-Refactor
- `ui/` — UI/UX 設計 + React コンポーネント
- `review/` — コードレビュー（OWASP + Google 実践）
- `deploy/` — 継続的デリバリー
- `sre/` — SLO/SLI/エラーバジェット
- `observe/` — ログ・メトリクス・トレース三本柱
- `security/` — Shift-Left + STRIDE 脅威モデリング
- `ddd/` — ユビキタス言語・境界づけられたコンテキスト
- `refactor/` — Fowler のカタログ + Feathers のレガシーコード手法

#### Subagents (4)
- `supervisor.md` — セッション常駐の監視役。意図分類・危険信号検知・スキル起動判断
- `review.md` — memory 付きコードレビュー拡張版
- `deploy.md` — memory + `permissionMode: default` 付きデプロイ拡張版
- `ddd.md` — memory でユビキタス言語を永続化

#### Hooks (3)
- `guard-bash.sh` — PreToolUse: 破壊的 Bash コマンドをブロック
- `guard-write.sh` — PreToolUse: 危険な Write/Edit をブロック
- `suggest-sdlc.sh` — UserPromptSubmit: 開発タスクで `/sdlc` を推奨

#### Scripts & Docs
- `scripts/install.sh` — バックアップ付きインストールスクリプト
- `hooks/settings-snippet.json` — `settings.json` 統合テンプレート
- `docs/design-decisions.md` — 設計判断の記録
- `docs/pretooluse-guards.md` — ガード仕様と誤検知対応

### Security

- シークレット（API キー、パスワード、トークン）の埋め込みなし
- ハードコードされた IP アドレス・ホスト名なし
- プロジェクト固有識別子は匿名化済み（`myapp_prod` / `myapp_test` 等のプレースホルダ）
