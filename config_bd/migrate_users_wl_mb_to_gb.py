"""
Однократная миграция: trafic_wl и limit_wl из MB в GB (деление на 1024).

Запуск из корня проекта:
  python -m config_bd.migrate_users_wl_mb_to_gb
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

_root = Path(__file__).resolve().parent.parent
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

from sqlalchemy import text

from config_bd.models import engine

# Если max(limit_wl) > 50 — значения ещё в MB (162 MB лимит и т.п.).
_MB_TO_GB_THRESHOLD = 50.0


async def migrate() -> None:
    async with engine.begin() as conn:
        table_check = await conn.execute(
            text("SELECT name FROM sqlite_master WHERE type='table' AND name='users'")
        )
        if not table_check.fetchone():
            return

        cols = await conn.execute(text("PRAGMA table_info(users)"))
        col_names = {row[1] for row in cols.fetchall()}
        if "limit_wl" not in col_names or "trafic_wl" not in col_names:
            return

        row = await conn.execute(text("SELECT MAX(limit_wl) FROM users"))
        max_limit = float(row.scalar() or 0.0)
        if max_limit <= _MB_TO_GB_THRESHOLD:
            print("SKIP: trafic_wl/limit_wl уже в GB (max limit_wl <= 50).")
            return

        await conn.execute(
            text("UPDATE users SET trafic_wl = ROUND(trafic_wl / 1024.0, 2) WHERE trafic_wl != 0")
        )
        await conn.execute(
            text("UPDATE users SET limit_wl = ROUND(limit_wl / 1024.0, 2) WHERE limit_wl != 0")
        )

    print("OK: trafic_wl, limit_wl converted MB -> GB.")


def main() -> None:
    asyncio.run(migrate())


if __name__ == "__main__":
    main()
