"""
Удаляет записи payments_fk_sbp с id в заданном диапазоне (включительно).

Запуск из корня проекта:
  python config_bd/delete_fk_payments_by_id.py
  python config_bd/delete_fk_payments_by_id.py --dry-run
  python config_bd/delete_fk_payments_by_id.py --from-id 8007 --to-id 8159
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path
from typing import Any, Dict

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config_bd.import_from_excel import DB_PATH  # noqa: E402


def delete_fk_by_id_range(
    *,
    from_id: int,
    to_id: int,
    dry_run: bool,
) -> Dict[str, Any]:
    if from_id > to_id:
        raise ValueError(f"from_id ({from_id}) > to_id ({to_id})")

    conn = sqlite3.connect(DB_PATH)
    try:
        preview = conn.execute(
            """
            SELECT count(1), coalesce(sum(amount), 0),
                   min(id), max(id)
            FROM payments_fk_sbp
            WHERE id >= ? AND id <= ?
            """,
            (from_id, to_id),
        ).fetchone()

        samples = conn.execute(
            """
            SELECT id, user_id, amount, time_created, status, signature
            FROM payments_fk_sbp
            WHERE id >= ? AND id <= ?
            ORDER BY id
            LIMIT 5
            """,
            (from_id, to_id),
        ).fetchall()

        stats: Dict[str, Any] = {
            "from_id": from_id,
            "to_id": to_id,
            "count": int(preview[0] or 0),
            "sum_rub": int(preview[1] or 0),
            "min_id": preview[2],
            "max_id": preview[3],
            "samples": samples,
            "deleted": 0,
            "dry_run": dry_run,
        }

        if dry_run or stats["count"] == 0:
            return stats

        cur = conn.execute(
            "DELETE FROM payments_fk_sbp WHERE id >= ? AND id <= ?",
            (from_id, to_id),
        )
        conn.commit()
        stats["deleted"] = cur.rowcount
        return stats
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Delete payments_fk_sbp rows by id range (inclusive)"
    )
    parser.add_argument("--from-id", type=int, default=8007)
    parser.add_argument("--to-id", type=int, default=8159)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    stats = delete_fk_by_id_range(
        from_id=args.from_id,
        to_id=args.to_id,
        dry_run=args.dry_run,
    )

    print("=== Delete payments_fk_sbp by id ===")
    print(f"DB: {DB_PATH}")
    print(f"Range: id {stats['from_id']} .. {stats['to_id']} (inclusive)")
    print(f"Matched: {stats['count']} rows, sum {stats['sum_rub']} rub")
    print(f"min/max id in range: {stats['min_id']} / {stats['max_id']}")
    if stats["samples"]:
        print("Sample (up to 5):")
        for row in stats["samples"]:
            print(f"  {row}")
    if stats["dry_run"]:
        print("(dry-run - nothing deleted)")
    elif stats["count"] == 0:
        print("Nothing to delete")
    else:
        print(f"Deleted rows: {stats['deleted']}")


if __name__ == "__main__":
    main()
