"""
Дописывает payload в старые рекуррентные списания Platega
из подписки (или собирает из user_id/duration/amount).

Запуск из корня проекта:
  python -m config_bd.migrate_platega_recurent_payload
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

_root = Path(__file__).resolve().parent.parent
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

from sqlalchemy import or_, select

from config_bd.models import AsyncSessionLocal, PlategaAutopaySubscription, PlategaRecurent
from payments.platega_recurrent import build_recurrent_payload, resolve_charge_payload


def _empty_payload(raw) -> bool:
    return not str(raw or "").strip()


async def migrate() -> None:
    async with AsyncSessionLocal() as session:
        autos = list((await session.execute(select(PlategaAutopaySubscription))).scalars().all())
        auto_filled = 0
        for row in autos:
            if not _empty_payload(row.payload):
                continue
            row.payload = build_recurrent_payload(
                int(row.user_id),
                str(row.duration),
                int(row.amount),
                white=bool(row.white),
                source=row.source,
            )
            auto_filled += 1

        charges = list(
            (
                await session.execute(
                    select(PlategaRecurent).where(
                        or_(
                            PlategaRecurent.payload.is_(None),
                            PlategaRecurent.payload == "",
                        )
                    )
                )
            ).scalars().all()
        )
        by_sub = {row.subscription_id: row for row in autos}
        charge_filled = 0
        skipped = 0
        for charge in charges:
            if not _empty_payload(charge.payload):
                continue
            autopay = by_sub.get(charge.subscription_id)
            if autopay is None:
                skipped += 1
                continue
            charge.payload = resolve_charge_payload(autopay)
            charge_filled += 1
        await session.commit()

    print(
        f"OK: platega recurrent payload "
        f"(autopay filled={auto_filled}, charges filled={charge_filled}, skipped={skipped})."
    )


def main() -> None:
    asyncio.run(migrate())


if __name__ == "__main__":
    main()
