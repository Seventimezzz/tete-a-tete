#!/usr/bin/env bash
# Ставит плагины Claude Code, которые использует проект (CLAUDE.md, раздел «Оркестрация»).
set -euo pipefail

add_marketplace() {
  if ! claude plugin marketplace list | grep -q "$1"; then
    claude plugin marketplace add "$2"
  fi
}

add_marketplace superpowers-marketplace obra/superpowers-marketplace
add_marketplace claude-code-workflows wshobson/agents

plugins=(
  superpowers@superpowers-marketplace
  python-development@claude-code-workflows
  javascript-typescript@claude-code-workflows
  security-scanning@claude-code-workflows
  backend-api-security@claude-code-workflows
  frontend-mobile-security@claude-code-workflows
  comprehensive-review@claude-code-workflows
  cicd-automation@claude-code-workflows
)

for plugin in "${plugins[@]}"; do
  claude plugin install "$plugin" --scope project
done

echo "Готово. Перезапустите Claude Code, чтобы плагины загрузились."
