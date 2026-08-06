#!/usr/bin/env bash
# Redescoberta + ranking semanal de carteiras (replica o cron "0 3 * * 1"
# que o GitHub Actions fazia). Disparado pelo timer deploy/rank.timer.
set -uo pipefail
cd "$(dirname "$0")/.."

.venv/bin/python main.py discover
.venv/bin/python main.py rank
