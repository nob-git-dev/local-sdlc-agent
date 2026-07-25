#!/bin/bash
# Local SDLC Skills インストールスクリプト
#
# このスクリプトは skills/ と agents/ をエージェント設定ディレクトリに展開します。
# 既存のスキル/エージェントは上書きされるため、事前にバックアップしてください。

set -e

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
AGENT_CONFIG_DIR="${AGENT_CONFIG_DIR:-$HOME/.local-sdlc-agent}"

echo "Local SDLC Skills インストーラー"
echo "================================"
echo "リポジトリ: $REPO_DIR"
echo "インストール先: $AGENT_CONFIG_DIR"
echo ""

# バックアップ
if [ -d "$AGENT_CONFIG_DIR/skills" ] || [ -d "$AGENT_CONFIG_DIR/agents" ] || [ -d "$AGENT_CONFIG_DIR/hooks" ]; then
  BACKUP_DIR="$AGENT_CONFIG_DIR/.backup-$(date +%Y%m%d-%H%M%S)"
  echo "既存のスキル/エージェント/フックを $BACKUP_DIR にバックアップします..."
  mkdir -p "$BACKUP_DIR"
  [ -d "$AGENT_CONFIG_DIR/skills" ] && cp -r "$AGENT_CONFIG_DIR/skills" "$BACKUP_DIR/"
  [ -d "$AGENT_CONFIG_DIR/agents" ] && cp -r "$AGENT_CONFIG_DIR/agents" "$BACKUP_DIR/"
  [ -d "$AGENT_CONFIG_DIR/hooks" ] && cp -r "$AGENT_CONFIG_DIR/hooks" "$BACKUP_DIR/"
  echo "バックアップ完了"
  echo ""
fi

# インストール
mkdir -p "$AGENT_CONFIG_DIR/skills" "$AGENT_CONFIG_DIR/agents" "$AGENT_CONFIG_DIR/hooks"

echo "スキルをインストール中..."
cp -r "$REPO_DIR/skills/"* "$AGENT_CONFIG_DIR/skills/"
for skill in "$AGENT_CONFIG_DIR/skills/"*/; do
  echo "  - $(basename "$skill")"
done

echo ""
echo "エージェントをインストール中..."
cp -r "$REPO_DIR/agents/"* "$AGENT_CONFIG_DIR/agents/"
for agent in "$AGENT_CONFIG_DIR/agents/"*.md; do
  echo "  - $(basename "$agent" .md)"
done

echo ""
echo "フック（PreToolUse ガード）をインストール中..."
cp "$REPO_DIR/hooks/guard-bash.sh" "$AGENT_CONFIG_DIR/hooks/"
cp "$REPO_DIR/hooks/guard-write.sh" "$AGENT_CONFIG_DIR/hooks/"
cp "$REPO_DIR/hooks/suggest-sdlc.sh" "$AGENT_CONFIG_DIR/hooks/"
chmod +x \
  "$AGENT_CONFIG_DIR/hooks/guard-bash.sh" \
  "$AGENT_CONFIG_DIR/hooks/guard-write.sh" \
  "$AGENT_CONFIG_DIR/hooks/suggest-sdlc.sh"
echo "  - guard-bash.sh (Bash 危険操作のブロック)"
echo "  - guard-write.sh (Write/Edit 危険操作のブロック)"
echo "  - suggest-sdlc.sh (開発タスクの検知と安全なフローの提案)"

echo ""
echo "================================"
echo "インストール完了"
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo " 最後に1つだけ手動作業があります"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""
echo " $AGENT_CONFIG_DIR/settings.json を開いて、"
echo " 以下の内容を追加してください。"
echo ""
echo " ※ settings.json がまだない場合は、"
echo "   このファイルをそのまま新規作成してください。"
echo ""
echo "-------- ここからコピー --------"
cat << EOF
{
  "agent": "supervisor",
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Bash",
        "hooks": [
          {
            "type": "command",
            "command": "$AGENT_CONFIG_DIR/hooks/guard-bash.sh",
            "timeout": 3
          }
        ]
      },
      {
        "matcher": "Write|Edit",
        "hooks": [
          {
            "type": "command",
            "command": "$AGENT_CONFIG_DIR/hooks/guard-write.sh",
            "timeout": 3
          }
        ]
      }
    ],
    "UserPromptSubmit": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "$AGENT_CONFIG_DIR/hooks/suggest-sdlc.sh",
            "timeout": 3
          }
        ]
      }
    ]
  }
}
EOF
echo "-------- ここまでコピー --------"
echo ""
echo " ※ すでに settings.json に内容がある場合は、"
echo "   既存の {} の中に \"agent\" と \"hooks\" の部分だけ追加してください。"
echo ""
echo " 設定後、対応するエージェント実行環境の次回起動から有効になります。"
echo " 動作確認: 実行環境で supervisor agent を選択してください。"
echo ""
