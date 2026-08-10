#!/usr/bin/env bash
# Backfill trafic_wl / limit_wl в socialvpn.db на VPS.
# Запуск из любой директории:
#   bash scripts/backfill_wl_limits.sh

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if [[ ! -f "config_bd/socialvpn.db" ]]; then
  echo "ERROR: config_bd/socialvpn.db не найден (cwd=$ROOT)" >&2
  exit 1
fi

python3 -m config_bd.backfill_wl_limits
