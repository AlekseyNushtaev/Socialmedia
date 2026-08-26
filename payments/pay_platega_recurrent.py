"""Telegram-бот: рекуррентные СБП-подписки Platega."""
from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from bot import sql
from config import ADMIN_IDS, PLATEGA_API_KEY, PLATEGA_MERCHANT_ID
from keyboard import BTN_BACK, create_kb, keyboard_payment_sbp
from lexicon import dct_desc, dct_price, lexicon
from logging_config import logger
from payments.payload_source import BOT
from payments.platega_recurrent import cancel_user_autopay, create_recurrent_payment, is_recurrent_tariff
from wl_traffic.texts import format_pro_payment_link
from payments.tariff_gate import panel_days_from_tariff_key, tariff_key_from_callback, tariff_period_label

router = Router()


@router.message(Command(commands=['sub']))
async def cmd_cancel_autopay(message: Message):
    """Отмена автоплатежа Platega СБП у клиента."""
    user_id = message.from_user.id
    active = await sql.get_active_platega_autopay(user_id)
    if not active:
        await message.answer(lexicon['autopay_cancel_none'], reply_markup=create_kb(1, back_to_main=BTN_BACK))
        return
    cancelled = await cancel_user_autopay(user_id, reason='user_command')
    if cancelled:
        period = tariff_period_label(panel_days_from_tariff_key(active.duration))
        await message.answer(
            lexicon['autopay_cancel_ok'].format(period),
            reply_markup=create_kb(1, back_to_main=BTN_BACK),
        )
    else:
        await message.answer(lexicon['autopay_cancel_none'], reply_markup=create_kb(1, back_to_main=BTN_BACK))


@router.callback_query(F.data.startswith('platega_rec_'))
async def process_platega_recurrent(callback: CallbackQuery):
    await callback.answer()
    if not PLATEGA_API_KEY or not PLATEGA_MERCHANT_ID:
        await callback.message.answer(
            lexicon['error_payment'],
            reply_markup=create_kb(1, back_to_main=BTN_BACK),
        )
        return

    raw = callback.data.replace('platega_rec_', '')
    tariff_key = tariff_key_from_callback(raw)
    duration = tariff_key.replace('white_', '')
    if 'old' in duration:
        duration = duration.replace('old', '')

    if not is_recurrent_tariff(duration):
        await callback.message.answer(
            'Автоплатёж доступен только для тарифов 7 дней, 1 месяц и 1 год.',
            reply_markup=create_kb(1, back_to_main=BTN_BACK),
        )
        return

    rub_amount = dct_price.get(duration) or dct_price.get(tariff_key)
    if rub_amount is None:
        await callback.message.answer(lexicon['error_payment'], reply_markup=create_kb(1, back_to_main=BTN_BACK))
        return
    if callback.from_user.id in ADMIN_IDS:
        rub_amount = 1

    user_id = callback.from_user.id
    payment_info = await create_recurrent_payment(
        user_id=user_id,
        duration=duration,
        amount=int(rub_amount),
        description=dct_desc.get(duration, dct_desc.get(tariff_key, 'Подписка')),
        white=False,
        source=BOT,
    )

    if payment_info.get('status') == 'rate_limited':
        await callback.message.answer(
            lexicon['payment_too_many_pending'].format(8),
            reply_markup=create_kb(1, back_to_main=BTN_BACK),
        )
        return

    if payment_info.get('status') in ('pending', 'PENDING') and payment_info.get('url'):
        days = panel_days_from_tariff_key(tariff_key)
        text = format_pro_payment_link(days)
        text += (
            '\n\nДля привязки счёта и оплаты перейдите по ссылке:\n'
            '<i>Подписка продлевается автоматически.</i>'
        )
        await callback.message.edit_text(
            text=text,
            reply_markup=keyboard_payment_sbp('⚡ Оплатить СБП', payment_info['url']),
            parse_mode='HTML',
        )
        logger.info('User {} created Platega recurrent {} ₽ duration={}', user_id, rub_amount, duration)
        return

    await callback.message.answer(lexicon['error_payment'], reply_markup=create_kb(1, back_to_main=BTN_BACK))
