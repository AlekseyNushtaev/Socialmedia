"""Обработчики профиля и покупки трафика Антиглушилка."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardMarkup

from bot import sql, x3
from keyboard import (
    BTN_BACK,
    CANCEL_AUTOPAY_CB,
    create_kb,
    keyboard_profile,
    keyboard_wl_traffic_payment_method,
    keyboard_wl_traffic_tariffs,
)
from lexicon import lexicon
from payments.platega_recurrent import cancel_user_autopay
from wl_traffic.constants import (
    BUY_VPN_CB,
    PROFILE_CB,
    WL_TRAFFIC_BUY_CB,
    WL_TRAFFIC_BUY_SUB_CB,
    WL_TRAFFIC_TARIFFS,
)
from wl_traffic.service import get_wl_used_gb_for_user

router = Router()


def _format_sub_end(user_data: tuple) -> str:
    sub_end = user_data[9] if len(user_data) > 9 else None
    if sub_end is None:
        return "—"
    if sub_end.tzinfo is None:
        aware = sub_end.replace(tzinfo=timezone.utc)
    else:
        aware = sub_end.astimezone(timezone.utc)
    if aware <= datetime.now(timezone.utc):
        return "истекла"
    return aware.strftime("%d.%m.%Y")


def _format_autopay(row) -> str:
    if row is None or getattr(row, "status", None) != "active":
        return lexicon["profile_autopay_off"]
    next_at = getattr(row, "next_charge_at", None)
    date_s = next_at.strftime("%d.%m.%Y") if next_at else "—"
    return lexicon["profile_autopay_next"].format(amount=row.amount, date=date_s)


async def _build_profile(uid: int) -> Optional[tuple[str, InlineKeyboardMarkup]]:
    user_data = await sql.get_user(uid)
    if not user_data:
        return None

    used_gb, limit_gb = await sql.get_wl_limits(uid)
    used_gb = await get_wl_used_gb_for_user(x3, uid, used_gb)
    remaining_gb = max(0.0, round(limit_gb - used_gb, 2))
    autopay = await sql.get_status_active_platega_autopay(uid)

    text = lexicon["user_profile"].format(
        sub_end=_format_sub_end(user_data),
        autopay=_format_autopay(autopay),
        limit_gb=limit_gb,
        used_gb=used_gb,
        remaining_gb=remaining_gb,
    )
    return text, keyboard_profile(has_active_autopay=autopay is not None)


@router.callback_query(F.data == PROFILE_CB)
async def user_profile_cb(callback: CallbackQuery):
    await callback.answer()
    content = await _build_profile(callback.from_user.id)
    if not content:
        await callback.message.answer(
            "❌ Профиль не найден.",
            reply_markup=create_kb(1, back_to_main=BTN_BACK),
        )
        return
    text, markup = content
    await callback.message.answer(text=text, parse_mode="HTML", reply_markup=markup)


@router.callback_query(F.data == CANCEL_AUTOPAY_CB)
async def cancel_autopay_cb(callback: CallbackQuery):
    uid = callback.from_user.id
    active = await sql.get_status_active_platega_autopay(uid)
    if not active:
        await callback.answer(lexicon["autopay_cancel_none"], show_alert=True)
        return

    cancelled = await cancel_user_autopay(
        uid,
        reason="user_profile",
        require_api_success=True,
        statuses=("active",),
    )
    if not cancelled:
        await callback.answer(lexicon["autopay_cancel_fail"], show_alert=True)
        return

    await callback.answer()
    content = await _build_profile(uid)
    if not content:
        return
    text, markup = content
    try:
        await callback.message.edit_text(text=text, parse_mode="HTML", reply_markup=markup)
    except Exception:
        await callback.message.answer(text=text, parse_mode="HTML", reply_markup=markup)


@router.callback_query(F.data.in_({WL_TRAFFIC_BUY_CB, WL_TRAFFIC_BUY_SUB_CB}))
async def wl_traffic_buy_cb(callback: CallbackQuery):
    back_callback = PROFILE_CB if callback.data == WL_TRAFFIC_BUY_CB else BUY_VPN_CB
    await callback.answer()
    await callback.message.answer(
        text=lexicon["wl_traffic_choose_pack"],
        parse_mode="HTML",
        reply_markup=keyboard_wl_traffic_tariffs(back_callback=back_callback),
    )


@router.callback_query(F.data.regexp(r"^wl_traffic(_sub)?_\d+$"))
async def wl_traffic_tariff_cb(callback: CallbackQuery):
    await callback.answer()
    data = callback.data or ""
    from_sub = data.startswith("wl_traffic_sub_")
    gb = data.rsplit("_", 1)[-1]
    if gb not in WL_TRAFFIC_TARIFFS:
        return

    price = WL_TRAFFIC_TARIFFS[gb]
    back_cb = WL_TRAFFIC_BUY_SUB_CB if from_sub else WL_TRAFFIC_BUY_CB
    await callback.message.answer(
        text=lexicon["wl_traffic_payment_intro"].format(gb=gb, price=price),
        parse_mode="HTML",
        reply_markup=keyboard_wl_traffic_payment_method(gb, back_callback=back_cb),
    )
