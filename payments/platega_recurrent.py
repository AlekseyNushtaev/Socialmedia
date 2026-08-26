"""Рекуррентные СБП-подписки Platega.io: создание, отмена, webhooks."""
from __future__ import annotations

import hmac
from datetime import datetime, timezone
from typing import Any, Dict, Literal, Optional, Sequence

import aiohttp

from bot import bot, sql
from config import BOT_URL, CHECKER_ID, PLATEGA_API_KEY, PLATEGA_MERCHANT_ID
from logging_config import logger
from payments.payment_limits import payment_creation_allowed
from payments.process_payload import process_confirmed_payment
from payments.tariff_gate import tariff_period_label

RECURRENT_TARIFFS = frozenset({'7', '30', '365'})
RECURRENT_METHOD = 'platega_rec'

# Platega interval: 1=день, 2=неделя, 3=месяц, 4=год
PLATEGA_INTERVAL_BY_DURATION: Dict[str, int] = {
    '7': 2,
    '30': 3,
    '365': 4,
}

_SUBSCRIPTION_STATUS_MAP = {
    'SUBSCRIPTION_ACTIVATED': 'active',
    'SUBSCRIPTION_PAST_DUE': 'past_due',
    'SUBSCRIPTION_CANCELLED': 'cancelled',
    'SUBSCRIPTION_FAILED': 'failed',
}


def is_recurrent_tariff(duration: str) -> bool:
    key = str(duration).replace('r_', '').replace('old', '')
    if key.startswith('white'):
        return False
    return key in RECURRENT_TARIFFS


def _parse_iso_dt(raw: Optional[str]) -> Optional[datetime]:
    if not raw:
        return None
    s = str(raw).strip()
    if s.endswith('Z'):
        s = s[:-1] + '+00:00'
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is not None:
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _webhook_field(data: Dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in data:
            return data[key]
        low = key.lower()
        for k, v in data.items():
            if k.lower() == low:
                return v
    return None


def verify_platega_webhook_headers(headers: Dict[str, str]) -> bool:
    if not PLATEGA_MERCHANT_ID or not PLATEGA_API_KEY:
        return False
    merchant = headers.get('X-MerchantId') or headers.get('x-merchantid') or ''
    secret = headers.get('X-Secret') or headers.get('x-secret') or ''
    return (
        hmac.compare_digest(merchant, PLATEGA_MERCHANT_ID)
        and hmac.compare_digest(secret, PLATEGA_API_KEY)
    )


async def _notify_checker(text: str) -> None:
    if CHECKER_ID is None:
        return
    try:
        await bot.send_message(chat_id=CHECKER_ID, text=text)
    except Exception as e:
        logger.error('Platega recurrent: не удалось уведомить CHECKER_ID: {}', e)


def _format_next_charge(next_charge_at: Optional[datetime]) -> str:
    if not next_charge_at:
        return '—'
    return next_charge_at.strftime('%d.%m.%Y %H:%M')


def build_recurrent_payload(
    user_id: int,
    duration: str,
    amount: int,
    *,
    white: bool = False,
    source: Optional[str] = None,
) -> str:
    tail = f",source:{source}" if source else ""
    return (
        f"user_id:{user_id},duration:{duration},white:{white},gift:False,"
        f"method:{RECURRENT_METHOD},amount:{amount},recurrent:1{tail}"
    )


class PlategaRecurrentClient:
    base_url = 'https://app.platega.io'

    def __init__(self, api_key: str, merchant_id: str):
        self.api_key = api_key
        self.merchant_id = merchant_id
        self.headers = {
            'X-Secret': api_key,
            'X-MerchantId': merchant_id,
            'Content-Type': 'application/json',
        }

    async def create_subscription(
        self,
        amount: int,
        interval: int,
        description: str,
        payload: Optional[str] = None,
    ) -> Dict[str, Any]:
        url = f'{self.base_url}/transaction/process'
        body: Dict[str, Any] = {
            'paymentMethod': 6,
            'paymentDetails': {
                'amount': int(amount),
                'currency': 'RUB',
                'interval': interval,
            },
            'description': description,
            'return': BOT_URL or 'https://t.me/',
            'failedUrl': BOT_URL or 'https://t.me/',
        }
        if payload:
            body['payload'] = payload
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=body, headers=self.headers) as response:
                text = await response.text()
                if response.status != 200:
                    logger.error('Platega create_subscription HTTP {}: {}', response.status, text[:500])
                    raise RuntimeError(f'Platega subscription error {response.status}')
                return await response.json()

    async def cancel_subscription(self, subscription_id: str) -> Dict[str, Any]:
        url = f'{self.base_url}/subscription/{subscription_id}/cancel'
        async with aiohttp.ClientSession() as session:
            async with session.post(url, headers=self.headers) as response:
                text = await response.text()
                if response.status != 200:
                    logger.error(
                        'Platega cancel_subscription {} HTTP {}: {}',
                        subscription_id, response.status, text[:500],
                    )
                    raise RuntimeError(f'Platega cancel error {response.status}')
                if not text:
                    return {'subscriptionId': subscription_id, 'status': 'cancelled'}
                return await response.json()


async def _cancel_platega_autopay_row(
    row, reason: str, *, require_api_success: bool = False,
) -> bool:
    """Отмена одной автоподписки в Platega и БД."""
    if row.status == 'cancelled':
        return False
    api_ok = False
    if PLATEGA_API_KEY and PLATEGA_MERCHANT_ID:
        try:
            client = PlategaRecurrentClient(PLATEGA_API_KEY, PLATEGA_MERCHANT_ID)
            await client.cancel_subscription(row.subscription_id)
            api_ok = True
        except Exception as e:
            logger.error(
                'Platega cancel sub={} user={}: {}',
                row.subscription_id, row.user_id, e,
            )
    if require_api_success and not api_ok:
        return False
    await sql.update_platega_autopay_status(row.subscription_id, 'cancelled', cancel_reason=reason)
    logger.info(
        'Autopay cancelled user={} sub={} reason={}',
        row.user_id, row.subscription_id, reason,
    )
    reason_labels = {
        'user_command': 'команда /sub',
        'user_profile': 'кнопка в профиле',
        'manual_payment': 'разовая оплата подписки',
        'new_recurrent': 'новый автоплатёж (первое списание)',
        'manual': 'вручную',
        'provider_cancelled': 'отмена у провайдера',
    }
    await _notify_checker(
        f'🔄 Автоплатёж СБП 🛑 отменён\n'
        f'user_id: {row.user_id}\n'
        f'Тариф: {tariff_period_label(row.duration)}\n'
        f'sub: {row.subscription_id}\n'
        f'Причина: {reason_labels.get(reason, reason)}'
    )
    return True


async def cancel_superseded_autopays(user_id: int, keep_subscription_id: str) -> None:
    """Отменяет другие автоподписки пользователя после успешной активации новой."""
    rows = await sql.list_active_platega_autopays(user_id)
    for row in rows:
        if row.subscription_id != keep_subscription_id:
            await _cancel_platega_autopay_row(row, reason='new_recurrent')


async def cancel_user_autopay(
    user_id: int,
    reason: str = 'manual',
    *,
    require_api_success: bool = False,
    statuses: Optional[Sequence[str]] = None,
) -> bool:
    """Отменяет все активные/ожидающие автоподписки Platega у пользователя."""
    rows = await sql.list_active_platega_autopays(user_id)
    if statuses is not None:
        rows = [row for row in rows if row.status in statuses]
    if not rows:
        return False
    any_ok = False
    for row in rows:
        if await _cancel_platega_autopay_row(
            row, reason=reason, require_api_success=require_api_success,
        ):
            any_ok = True
    return any_ok


async def create_recurrent_payment(
    *,
    user_id: int,
    duration: str,
    amount: int,
    description: str,
    white: bool = False,
    source: Optional[str] = None,
) -> Dict[str, Any]:
    if not is_recurrent_tariff(duration):
        return {'status': 'error', 'url': '', 'id': '', 'message': 'unsupported_tariff'}
    if not PLATEGA_API_KEY or not PLATEGA_MERCHANT_ID:
        logger.error('Platega recurrent: не заданы PLATEGA_API_KEY / PLATEGA_MERCHANT_ID')
        return {'status': 'error', 'url': '', 'id': ''}
    if not await payment_creation_allowed(user_id):
        return {'status': 'rate_limited', 'url': '', 'id': ''}

    interval = PLATEGA_INTERVAL_BY_DURATION.get(duration)
    if interval is None:
        return {'status': 'error', 'url': '', 'id': ''}

    payload = build_recurrent_payload(user_id, duration, amount, white=white, source=source)
    client = PlategaRecurrentClient(PLATEGA_API_KEY, PLATEGA_MERCHANT_ID)
    try:
        result = await client.create_subscription(amount, interval, description, payload=payload)
    except Exception as e:
        logger.error('Platega create_recurrent_payment user={}: {}', user_id, e)
        return {'status': 'error', 'url': '', 'id': ''}

    subscription_id = str(result.get('transactionId') or '').strip()
    redirect = str(result.get('redirect') or '').strip()
    status = str(result.get('status') or 'PENDING').lower()
    if not subscription_id or not redirect:
        logger.error('Platega create_recurrent_payment invalid response: {}', result)
        return {'status': 'error', 'url': '', 'id': ''}

    await sql.add_platega_autopay_pending(
        user_id,
        subscription_id,
        duration,
        amount,
        payload,
        white=white,
        source=source,
    )
    logger.info(
        'Platega recurrent created user={} sub={} duration={} amount={}',
        user_id, subscription_id, duration, amount,
    )
    await _notify_checker(
        f'🔄 Автоплатёж СБП — создан\n'
        f'user_id: {user_id}\n'
        f'Тариф: {tariff_period_label(duration)}\n'
        f'Сумма: {amount} ₽\n'
        f'sub: {subscription_id}\n'
        f'Статус: ожидает привязки счёта'
    )
    return {'status': status, 'url': redirect, 'id': subscription_id}


async def handle_subscription_status_webhook(data: Dict[str, Any]) -> None:
    subscription_id = str(_webhook_field(data, 'SubscriptionId', 'subscriptionId') or '').strip()
    status_raw = str(_webhook_field(data, 'Status', 'status') or '').strip().upper()
    next_charge_at = _parse_iso_dt(_webhook_field(data, 'NextChargeAt', 'nextChargeAt'))

    if not subscription_id:
        logger.warning('Platega status webhook without SubscriptionId: {}', data)
        return

    mapped = _SUBSCRIPTION_STATUS_MAP.get(status_raw)
    if not mapped:
        logger.info('Platega status webhook ignored status={} sub={}', status_raw, subscription_id)
        return

    row = await sql.get_platega_autopay_by_subscription_id(subscription_id)
    if not row:
        logger.warning('Platega status webhook unknown sub={}', subscription_id)
        return

    prev_status = row.status
    cancel_reason = 'provider_cancelled' if mapped == 'cancelled' else None
    await sql.update_platega_autopay_status(
        subscription_id,
        mapped,
        next_charge_at=next_charge_at,
        cancel_reason=cancel_reason,
    )
    logger.info('Platega subscription status sub={} -> {}', subscription_id, mapped)

    if mapped == 'cancelled' and prev_status == 'cancelled':
        return

    status_labels = {
        'active': '✅ активирована',
        'past_due': '⚠️ просрочена (past_due)',
        'cancelled': '🛑 отменена',
        'failed': '❌ не удалось привязать',
    }
    await _notify_checker(
        f'🔄 Автоплатёж СБП — {status_labels.get(mapped, mapped)}\n'
        f'user_id: {row.user_id}\n'
        f'Тариф: {tariff_period_label(row.duration)}\n'
        f'Сумма: {row.amount} ₽\n'
        f'sub: {subscription_id}\n'
        f'След. списание: {_format_next_charge(next_charge_at)}'
    )


async def handle_subscription_charge_webhook(data: Dict[str, Any]) -> None:
    transaction_id = str(_webhook_field(data, 'Id', 'id') or '').strip()
    subscription_id = str(_webhook_field(data, 'SubscriptionId', 'subscriptionId') or '').strip()
    status_raw = str(_webhook_field(data, 'Status', 'status') or '').strip().upper()
    amount = int(_webhook_field(data, 'Amount', 'amount') or 0)
    currency = str(_webhook_field(data, 'Currency', 'currency') or 'RUB')
    payment_method = _webhook_field(data, 'PaymentMethod', 'paymentMethod')
    payload_from_hook = _webhook_field(data, 'Payload', 'payload')
    next_charge_at = _parse_iso_dt(_webhook_field(data, 'NextChargeAt', 'nextChargeAt'))

    if not transaction_id or not subscription_id:
        logger.warning('Platega charge webhook missing ids: {}', data)
        return

    autopay = await sql.get_platega_autopay_by_subscription_id(subscription_id)
    if not autopay:
        logger.warning('Platega charge webhook unknown sub={}', subscription_id)
        return

    if autopay.status == 'cancelled':
        logger.info(
            'Platega charge webhook ignored (sub already cancelled): sub={} tx={} status={}',
            subscription_id, transaction_id, status_raw,
        )
        return

    pm_int: Optional[int] = None
    if payment_method is not None:
        try:
            pm_int = int(payment_method)
        except (TypeError, ValueError):
            pm_int = None

    row = await sql.add_platega_recurent_payment(
        user_id=int(autopay.user_id),
        subscription_id=subscription_id,
        transaction_id=transaction_id,
        amount=amount,
        currency=currency,
        status=status_raw,
        payment_method=pm_int,
        payload=str(payload_from_hook) if payload_from_hook else autopay.payload,
        next_charge_at=next_charge_at,
    )
    if row and row.processed:
        logger.info('Platega charge webhook duplicate processed tx={}', transaction_id)
        return

    if next_charge_at:
        await sql.update_platega_autopay_status(
            subscription_id, 'active', next_charge_at=next_charge_at,
        )

    if status_raw == 'CONFIRMED':
        is_first_charge = not await sql.platega_recurent_has_confirmed(subscription_id)
        pay_payload = autopay.payload or str(payload_from_hook or '')
        if pay_payload:
            ok = await process_confirmed_payment(pay_payload)
            if ok:
                await sql.mark_platega_recurent_processed(transaction_id)
                if is_first_charge:
                    await cancel_superseded_autopays(int(autopay.user_id), subscription_id)
                logger.info(
                    'Platega recurrent charge applied user={} tx={} amount={}',
                    autopay.user_id, transaction_id, amount,
                )
                await _notify_checker(
                    f'🔄 Автоплатёж СБП ✅ списание\n'
                    f'user_id: {autopay.user_id}\n'
                    f'Тариф: {tariff_period_label(autopay.duration)}\n'
                    f'Сумма: {amount} {currency}\n'
                    f'tx: {transaction_id}\n'
                    f'sub: {subscription_id}\n'
                    f'След. списание: {_format_next_charge(next_charge_at)}'
                )
            else:
                logger.error(
                    'Platega recurrent charge process_confirmed_payment failed user={} tx={}',
                    autopay.user_id, transaction_id,
                )
                await _notify_checker(
                    f'🔄 Автоплатёж СБП ⚠️ списание получено, VPN не продлён\n'
                    f'user_id: {autopay.user_id}\n'
                    f'Сумма: {amount} {currency}\n'
                    f'tx: {transaction_id}\n'
                    f'sub: {subscription_id}'
                )
    elif status_raw == 'CANCELED':
        await sql.update_platega_autopay_status(subscription_id, 'past_due', next_charge_at=None)
        logger.warning('Platega recurrent charge failed sub={} tx={}', subscription_id, transaction_id)
        await _notify_checker(
            f'🔄 Автоплатёж СБП ❌ списание не прошло\n'
            f'user_id: {autopay.user_id}\n'
            f'Тариф: {tariff_period_label(autopay.duration)}\n'
            f'Сумма: {amount} {currency}\n'
            f'tx: {transaction_id}\n'
            f'sub: {subscription_id}'
        )


def webhook_kind(data: Dict[str, Any]) -> Literal['status', 'charge', 'unknown']:
    status_raw = str(_webhook_field(data, 'Status', 'status') or '').strip().upper()
    if status_raw.startswith('SUBSCRIPTION_'):
        return 'status'
    if status_raw in ('CONFIRMED', 'CANCELED', 'PENDING', 'CHARGEBACKED'):
        if _webhook_field(data, 'SubscriptionId', 'subscriptionId'):
            return 'charge'
    return 'unknown'
