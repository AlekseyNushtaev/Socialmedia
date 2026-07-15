"""
Рандомно добавляет confirmed-платежи FreeKassa (~target ₽) на актуальных тарифах
в случайное время с 1 по 15 июля выбранного года.

Запуск из корня проекта:
  python config_bd/seed_fk_random_payments.py
  python config_bd/seed_fk_random_payments.py --target 100000 --year 2026
  python config_bd/seed_fk_random_payments.py --dry-run
"""
from __future__ import annotations

import argparse
import random
import secrets
import sqlite3
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Tuple

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config_bd.import_from_excel import DB_PATH, _dt_sql  # noqa: E402
from lexicon import dct_price  # noqa: E402

# Актуальные тарифы из клавиатуры (white_30 сейчас закомментирован).
CURRENT_TARIFFS: List[Tuple[str, int, bool]] = [
    (key, int(dct_price[key]), "white" in key)
    for key in ("7", "30", "90", "180", "365")
    if key in dct_price
]

FK_INSERT_COLS = [
    "user_id",
    "amount",
    "time_created",
    "is_gift",
    "status",
    "transaction_id",
    "fk_order_id",
    "payload",
    "nonce",
    "signature",
    "method",
]


def _random_july_dt(year: int) -> datetime:
    """Случайный момент с 1 по 15 июля включительно."""
    start = datetime(year, 7, 1, 0, 0, 0)
    end = datetime(year, 7, 15, 23, 59, 59)
    span = int((end - start).total_seconds())
    return start + timedelta(seconds=random.randint(0, span))


def _build_row(
    user_id: int,
    duration: str,
    amount: int,
    white: bool,
    when: datetime,
    nonce: int,
) -> Tuple[Any, ...]:
    ui_kind = random.choice(("sbp", "card"))
    method = "fk_qr_sbp" if ui_kind == "sbp" else "fk_qr_card"
    pm = "fk_sbp" if ui_kind == "sbp" else "fk_card"
    transaction_id = f"fk{user_id}n{nonce}"
    payload = (
        f"user_id:{user_id},duration:{duration},white:{white},"
        f"gift:False,method:{pm},amount:{amount},source:bot"
    )
    return (
        user_id,
        amount,
        _dt_sql(when),
        0,  # is_gift
        "confirmed",
        transaction_id,
        random.randint(10_000_000, 99_999_999),  # fk_order_id
        payload,
        nonce,
        secrets.token_hex(32),  # signature (как sha256 hex)
        method,
    )


def seed_payments(
    *,
    target: int,
    year: int,
    dry_run: bool,
) -> Dict[str, Any]:
    if not CURRENT_TARIFFS:
        raise RuntimeError("Нет актуальных тарифов в dct_price")

    conn = sqlite3.connect(DB_PATH)
    try:
        users = [int(r[0]) for r in conn.execute("SELECT user_id FROM users").fetchall()]
        if not users:
            raise RuntimeError("В таблице users нет пользователей")

        rows: List[Tuple[Any, ...]] = []
        total = 0
        by_tariff: Dict[str, int] = {k: 0 for k, _, _ in CURRENT_TARIFFS}
        nonce_base = time.time_ns() // 1000

        # Пока не набрали ~target (допустимый перелёт — максимум одного тарифа).
        while total < target:
            duration, amount, white = random.choice(CURRENT_TARIFFS)
            if total + amount > target and total > 0:
                # Если уже близко к цели — пробуем более мелкий тариф, иначе останавливаемся.
                smaller = [t for t in CURRENT_TARIFFS if t[1] <= target - total]
                if not smaller:
                    break
                duration, amount, white = random.choice(smaller)
                if total + amount > target:
                    break

            user_id = random.choice(users)
            when = _random_july_dt(year)
            nonce = nonce_base + len(rows)
            rows.append(_build_row(user_id, duration, amount, white, when, nonce))
            total += amount
            by_tariff[duration] = by_tariff.get(duration, 0) + 1

        # На всякий случай упорядочим по времени
        rows.sort(key=lambda r: r[2])

        stats = {
            "users_pool": len(users),
            "payments": len(rows),
            "total_rub": total,
            "target": target,
            "year": year,
            "by_tariff": by_tariff,
            "dry_run": dry_run,
        }

        if dry_run or not rows:
            return stats

        placeholders = ", ".join("?" * len(FK_INSERT_COLS))
        col_list = ", ".join(FK_INSERT_COLS)
        sql = f"INSERT INTO payments_fk_sbp ({col_list}) VALUES ({placeholders})"
        conn.executemany(sql, rows)
        conn.commit()
        return stats
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Seed random confirmed FreeKassa payments (~target ₽, 1–15 July)"
    )
    parser.add_argument("--target", type=int, default=100_000, help="Целевая сумма в ₽ (примерно)")
    parser.add_argument("--year", type=int, default=2026, help="Год для дат 1–15 июля")
    parser.add_argument("--dry-run", action="store_true", help="Только посчитать, не писать в БД")
    parser.add_argument("--seed", type=int, default=None, help="Фиксированный random seed")
    args = parser.parse_args()

    if args.seed is not None:
        random.seed(args.seed)

    stats = seed_payments(target=args.target, year=args.year, dry_run=args.dry_run)
    print("=== Seed FreeKassa confirmed ===")
    print(f"DB: {DB_PATH}")
    print(f"Users pool: {stats['users_pool']}")
    print(f"Payments: {stats['payments']}")
    print(f"Total rub: {stats['total_rub']} (target {stats['target']})")
    print(f"Period: 1-15 July {stats['year']}")
    print("By tariff (count):")
    for key, cnt in stats["by_tariff"].items():
        price = dct_price.get(key)
        print(f"  {key} ({price} rub): {cnt}")
    if stats["dry_run"]:
        print("(dry-run - nothing written to DB)")
    else:
        print("OK - inserted into payments_fk_sbp")


if __name__ == "__main__":
    main()
