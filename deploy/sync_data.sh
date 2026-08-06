#!/usr/bin/env bash
# Sincroniza data/ e reports/ para o GitHub, para o dashboard continuar a ler
# os dados ao vivo (o dashboard lê o raw do GitHub, não o VPS diretamente).
# Disparado pelo timer deploy/sync.timer.
#
# PRECISA de credenciais de push no VPS (configurar UMA vez, ver VPS.md secção
# "Dashboard ao vivo"):
#   git config --global credential.helper store
#   git config --global user.name  "testbot-vps"
#   git config --global user.email "bot@vps.local"
#   git remote set-url origin https://<SEU_USER>@github.com/RomelNeto/testbot.git
#   ... e na primeira vez, dar o Personal Access Token quando o git pedir.
#
# Sem credenciais, este script falha em silêncio (não parte nada).
set -uo pipefail
cd "$(dirname "$0")/.."

git add -A data reports 2>/dev/null || true
if git diff --cached --quiet; then
    exit 0
fi

git -c user.name="testbot-vps" -c user.email="bot@vps.local" \
    commit -m "bot: sync $(date -u '+%Y-%m-%d %H:%M UTC')" 2>/dev/null || exit 0

# Rebase leve antes do push para minimizar conflitos com o que vier do GitHub
git pull --rebase origin main 2>/dev/null || true
git push origin main 2>/dev/null || true
