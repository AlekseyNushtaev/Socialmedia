"""
Adds online.users_subscribed if missing.

Existing rows stay NULL (historical days had no snapshot of this metric).

Run from project root on VPS:
  python migrate_add_online_users_subscribed.py
"""
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "config_bd" / "socialvpn.db"

TABLE = "online"
COLUMN = "users_subscribed"
COLDEF = "INTEGER"


def main() -> None:
    if not DB_PATH.is_file():
        raise SystemExit(f"Database not found: {DB_PATH}")

    conn = sqlite3.connect(DB_PATH)
    try:
        existing = {row[1] for row in conn.execute(f"PRAGMA table_info({TABLE})").fetchall()}
        if COLUMN in existing:
            print(f"skip (exists): {TABLE}.{COLUMN}")
        else:
            conn.execute(f'ALTER TABLE "{TABLE}" ADD COLUMN "{COLUMN}" {COLDEF}')
            conn.commit()
            print(f"ok: {TABLE}.{COLUMN}")
    finally:
        conn.close()

    print("Done.")


if __name__ == "__main__":
    main()
