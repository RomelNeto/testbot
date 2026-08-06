#!/usr/bin/env bash
# Arranca o bot em loop contínuo, puxando primeiro o código mais recente.
# Usado pelo systemd (deploy/testbot.service). Também pode ser corrido à mão.
set -uo pipefail
cd "$(dirname "$0")/.."

mkdir -p logs

# 1) Guarda qualquer dado local pendente (data/, reports/) antes de atualizar,
#    para o git pull não falhar por causa de mudanças locais.
git add -A data reports 2>/dev/null || true
git -c user.name="testbot-vps" -c user.email="bot@vps.local" \
    commit -m "vps: dados pendentes antes do pull" 2>/dev/null || true

# 2) Puxa código novo (e dados novos do GitHub, se houver). Se falhar
#    (ex.: rede), continua com o código atual -- não derruba o bot.
git pull --rebase origin main 2>/dev/null \
    || echo "[run_bot] pull falhou; a continuar com o código atual" >&2

# 3) Loop contínuo do bot (não usa tmux; o systemd gere o processo).
exec .venv/bin/python main.py run
