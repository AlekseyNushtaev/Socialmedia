"""
HTTP API для кастомной страницы подписки: создание платежей FreeKassa (СБП/карта), Stars, CryptoBot.

Защита: заголовок Authorization: Bearer <SUB_PAGE_API_KEY> или X-Sub-Page-Api-Key
(переменная окружения SUB_PAGE_API_KEY в .env).
"""
from __future__ import annotations

import time
from typing import Annotated, Literal, Optional

from fastapi import Depends, FastAPI, HTTPException, Request, Security, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import APIKeyHeader
from pydantic import BaseModel, Field
from aiogram.types import LabeledPrice

from bot import bot
from config import (
    ADMIN_IDS,
    BOT_URL,
    CRYPTOBOT_API_TOKEN,
    API_FREEKASSA,
    SHOP_ID_FREEKASSA,
    SUB_PAGE_API_KEY,
    SUB_PAGE_CORS_ORIGINS,
)
from keyboard import keyboard_payment_stars
from lexicon import dct_desc, dct_price, lexicon
from logging_config import logger
from payments.pay_cryptobot import create_cryptobot_payment
from payments.pay_freekassa import pay as fk_pay
from payments.pay_stars import get_stars_amount

DurationId = Literal["7", "30", "90", "180", "365", "white_30"]

# Маркер в строке payload платежей, созданных через HTTP API страницы подписки (хранится в БД).
SUB_PAGE_PAYMENT_SOURCE = "subpage"

_rate_limits: dict[str, list[float]] = {}


def _rate_check(key: str, max_requests: int, window_sec: int) -> bool:
    now = time.time()
    timestamps = _rate_limits.get(key, [])
    timestamps = [t for t in timestamps if now - t < window_sec]
    if len(timestamps) >= max_requests:
        _rate_limits[key] = timestamps
        return False
    timestamps.append(now)
    _rate_limits[key] = timestamps
    return True


def _rate_limit_or_raise(request: Request, action: str, max_req: int = 20, window: int = 300) -> None:
    client_ip = request.headers.get("x-real-ip") or (request.client.host if request.client else "")
    key = f"{action}:{client_ip}"
    if not _rate_check(key, max_req, window):
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "Слишком много запросов. Подождите несколько минут.",
        )


def _parse_cors_origins(raw: Optional[str]) -> list[str]:
    if not raw or not raw.strip():
        return []
    return [x.strip() for x in raw.split(",") if x.strip()]


sub_page_api_key_header = APIKeyHeader(
    name="X-Sub-Page-Api-Key",
    scheme_name="SubPageApiKey",
    auto_error=False,
    description="То же значение, что в .env: SUB_PAGE_API_KEY",
)


async def require_sub_page_auth(
    request: Request,
    x_sub_page_key: Optional[str] = Security(sub_page_api_key_header),
) -> None:
    """
    Ключ в Swagger: кнопка Authorize → поле SubPageApiKey (заголовок X-Sub-Page-Api-Key).
    Дополнительно без схемы в OpenAPI: Authorization: Bearer <SUB_PAGE_API_KEY>.
    """
    if not SUB_PAGE_API_KEY:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Не задан SUB_PAGE_API_KEY — эндпоинты страницы подписки отключены.",
        )
    if x_sub_page_key == SUB_PAGE_API_KEY:
        return
    bearer = (request.headers.get("Authorization") or "").strip()
    if bearer.lower().startswith("bearer "):
        if bearer[7:].strip() == SUB_PAGE_API_KEY:
            return
    raise HTTPException(
        status.HTTP_401_UNAUTHORIZED,
        "Неверный или отсутствующий ключ. В Swagger нажмите Authorize и введите SUB_PAGE_API_KEY, "
        "или передайте заголовок Authorization: Bearer <ключ>.",
    )


SubPageAuth = Annotated[None, Depends(require_sub_page_auth)]


class SubPagePayIn(BaseModel):
    user_id: int = Field(..., description="Telegram user id")
    duration: DurationId


def _tariff_parts(duration_id: DurationId) -> tuple[str, str, bool]:
    """desc_key для dct_desc/dct_price, duration для payload панели, white."""
    desc_key = duration_id
    white = "white_" in duration_id
    d = duration_id.replace("white_", "", 1) if white else duration_id
    if "old" in d:
        d = d.replace("old", "")
    return desc_key, d, white


def _rub_for_user(user_id: int, duration_id: DurationId) -> int:
    rub = dct_price[duration_id]
    if user_id in ADMIN_IDS:
        return 1
    return rub


async def _bot_deeplink() -> str:
    if BOT_URL and BOT_URL.strip():
        return BOT_URL.rstrip("/")
    try:
        me = await bot.get_me()
        if me.username:
            return f"https://t.me/{me.username}"
    except Exception as e:
        logger.warning("web_api: не удалось получить username бота: {}", e)
    return "https://t.me/"


app = FastAPI(
    title="SocialmediaVPN — API страницы подписки",
    version="1",
    swagger_ui_parameters={"persistAuthorization": True},
)

_cors = _parse_cors_origins(SUB_PAGE_CORS_ORIGINS)
_cors_origins = _cors if _cors else ["*"]
_cors_credentials = bool(_cors)
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=_cors_credentials,
    allow_methods=["POST", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-Sub-Page-Api-Key"],
)


@app.post("/api/v1/sub_page/pay/fk_sbp")
async def sub_page_pay_fk_sbp(body: SubPagePayIn, request: Request, _: SubPageAuth):
    _rate_limit_or_raise(request, "fk_sbp")
    if not API_FREEKASSA or SHOP_ID_FREEKASSA is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "FreeKassa не настроена")

    desc_key, duration_panel, white = _tariff_parts(body.duration)
    rub = _rub_for_user(body.user_id, body.duration)
    uid_str = str(body.user_id)

    result = await fk_pay(
        val=str(rub),
        des=dct_desc[desc_key],
        user_id=uid_str,
        duration=duration_panel,
        white=white,
        ui_kind="sbp",
        source=SUB_PAGE_PAYMENT_SOURCE,
    )
    if result.get("status") != "pending":
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Не удалось создать платёж FreeKassa (СБП)")

    return {
        "payment_url": result.get("url") or "",
        "payment_id": result.get("id") or "",
    }


@app.post("/api/v1/sub_page/pay/fk_card")
async def sub_page_pay_fk_card(body: SubPagePayIn, request: Request, _: SubPageAuth):
    _rate_limit_or_raise(request, "fk_card")
    if not API_FREEKASSA or SHOP_ID_FREEKASSA is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "FreeKassa не настроена")

    desc_key, duration_panel, white = _tariff_parts(body.duration)
    rub = _rub_for_user(body.user_id, body.duration)
    uid_str = str(body.user_id)

    result = await fk_pay(
        val=str(rub),
        des=dct_desc[desc_key],
        user_id=uid_str,
        duration=duration_panel,
        white=white,
        ui_kind="card",
        source=SUB_PAGE_PAYMENT_SOURCE,
    )
    if result.get("status") != "pending":
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Не удалось создать платёж FreeKassa (карта)")

    return {
        "payment_url": result.get("url") or "",
        "payment_id": result.get("id") or "",
    }


@app.post("/api/v1/sub_page/pay/stars")
async def sub_page_pay_stars(body: SubPagePayIn, request: Request, _: SubPageAuth):
    _rate_limit_or_raise(request, "stars")

    desc_key, duration_panel, white = _tariff_parts(body.duration)
    stars_amount = int(get_stars_amount("Stars", body.duration))
    if body.user_id in ADMIN_IDS:
        stars_amount = 1

    gift_flag = False
    payload = (
        f"user_id:{body.user_id},duration:{duration_panel},white:{white},"
        f"gift:{gift_flag},method:stars,amount:{stars_amount},source:{SUB_PAGE_PAYMENT_SOURCE}"
    )
    prices = [LabeledPrice(label="XTR", amount=stars_amount)]
    title = f"Оплата подписки на {duration_panel} дней."
    description = lexicon["payment_link_white"] if white else lexicon["payment_link"]

    try:
        await bot.send_invoice(
            body.user_id,
            title=title,
            description=description,
            prices=prices,
            provider_token="",
            payload=payload,
            currency="XTR",
            reply_markup=keyboard_payment_stars(stars_amount),
        )
    except Exception as e:
        logger.error("web_api stars send_invoice user_id={}: {}", body.user_id, e)
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            "Не удалось отправить счёт в Telegram (возможно, бот заблокирован или нет диалога).",
        )

    bot_url = await _bot_deeplink()
    return {"bot_url": bot_url, "stars_amount": stars_amount}


@app.post("/api/v1/sub_page/pay/cryptobot")
async def sub_page_pay_cryptobot(body: SubPagePayIn, request: Request, _: SubPageAuth):
    _rate_limit_or_raise(request, "cryptobot")
    if not CRYPTOBOT_API_TOKEN:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "CryptoBot не настроен")

    desc_key, duration_panel, white = _tariff_parts(body.duration)
    rub = _rub_for_user(body.user_id, body.duration)

    result = await create_cryptobot_payment(
        rub_amount=rub,
        description=dct_desc[desc_key],
        user_id=body.user_id,
        duration=duration_panel,
        white=white,
        is_gift=False,
        source=SUB_PAGE_PAYMENT_SOURCE,
    )
    if result.get("status") != "pending":
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Не удалось создать счёт CryptoBot")

    return {
        "payment_url": result.get("url") or "",
        "invoice_id": result.get("invoice_id"),
    }
