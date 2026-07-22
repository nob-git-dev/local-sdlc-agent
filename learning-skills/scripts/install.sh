#!/bin/bash
# Claude Learning Skills インストールスクリプト
#
# このスクリプトは skills/（3つの学習パイプラインスキル）を ~/.claude/skills/ に展開します。
# 同名のスキルが既にある場合は上書きされるため、事前に自動バックアップを取ります。
# CLAUDE.md・settings.json・他のスキルには一切触れません。

set -e

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
CLAUDE_DIR="${CLAUDE_DIR:-$HOME/.claude}"

echo "Claude Learning Skills インストーラー"
echo "===================================="
echo "リポジトリ: $REPO_DIR"
echo "インストール先: $CLAUDE_DIR/skills"
echo ""

SKILLS="post-project-learning-engine skill-proposal-engine skill-regression-checker"

# 既存の同名スキルだけをバックアップ（他のスキルには触れない）
NEED_BACKUP=0
for s in $SKILLS ; do
  [ -d "$CLAUDE_DIR/skills/$s" ] && NEED_BACKUP=1
done
if [ "$NEED_BACKUP" = "1" ]; then
  BACKUP_DIR="$CLAUDE_DIR/.backup-learning-$(date +%Y%m%d-%H%M%S)"
  echo "同名の既存スキルを $BACKUP_DIR にバックアップします..."
  mkdir -p "$BACKUP_DIR"
  for s in $SKILLS ; do
    [ -d "$CLAUDE_DIR/skills/$s" ] && cp -r "$CLAUDE_DIR/skills/$s" "$BACKUP_DIR/"
  done
  echo "バックアップ完了"
  echo ""
fi

# インストール
mkdir -p "$CLAUDE_DIR/skills"
echo "学習パイプラインスキルをインストール中..."
for s in $SKILLS ; do
  cp -r "$REPO_DIR/skills/$s" "$CLAUDE_DIR/skills/"
  echo "  - $s"
done

echo ""
echo "===================================="
echo "インストール完了"
echo ""
echo "これらのスキルは settings.json への追加は不要です。"
echo "次回 claude 起動時から、以下で呼び出せます。"
echo ""
echo "  /post-project-learning-engine   … プロジェクト完了後の学習ログ生成（第1段）"
echo "  /skill-proposal-engine          … 学習の棚卸し・スキル化提案（第2段）"
echo "  /skill-regression-checker       … 更新案の回帰・副作用検査（第3段）"
echo ""
echo "あるいは「今回のふりかえりして」「学習を棚卸しして」「この更新案を検査して」"
echo "といった自然な依頼でも自動起動します。"
echo ""
