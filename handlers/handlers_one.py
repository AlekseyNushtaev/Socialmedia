import asyncio

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from bot import bot, sql
from config import ADMIN_IDS
from logging_config import logger
from telegram_ids import is_telegram_chat_id

router = Router()

_INFO_TEXT = (
    "Уважаемые друзья!\n"
    "Мы столкнулись с аварией в датацентре.\n"
    "Проблема решается и вскоре ВПН заработает.\n"
    "В качестве компенсации добавим Вам 7 дней к подписке, "
    "как завершатся технические работы👌"
)


@router.message(Command(commands=["info"]))
async def info_broadcast(message: Message):
    """Рассылка пользователям in_panel=True, is_delete=False."""
    if message.from_user.id not in ADMIN_IDS:
        return

    user_ids = await sql.select_subscribe_yes()
    if not user_ids:
        await message.answer("Нет пользователей (in_panel=True, is_delete=False).")
        return

    await message.answer(
        f"⏳ /info: начинаю рассылку для {len(user_ids)} пользователей…"
    )

    admin_chat_id = message.chat.id
    sent = 0
    failed = 0
    skipped_non_tg = 0

    for user_id in user_ids:
        if not is_telegram_chat_id(user_id):
            skipped_non_tg += 1
            await asyncio.sleep(0.05)
            continue
        try:
            await bot.send_message(user_id, _INFO_TEXT)
            sent += 1
            if sent % 1000 == 0:
                try:
                    await bot.send_message(
                        admin_chat_id,
                        f"/info: отправлено сообщений — {sent}",
                    )
                except Exception as notify_err:
                    logger.warning(
                        "/info: не удалось отправить прогресс админу: %s",
                        notify_err,
                    )
        except Exception as e:
            failed += 1
            logger.warning("/info: не отправлено user_id=%s: %s", user_id, e)

        await asyncio.sleep(0.05)

    await message.answer(
        "Готово (/info).\n"
        f"• Отправлено: {sent}\n"
        f"• Ошибок: {failed}\n"
        f"• Пропущено (не Telegram chat_id): {skipped_non_tg}"
    )
