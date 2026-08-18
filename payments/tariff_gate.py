"""Ключи тарифов и маппинг в дни подписки для панели."""


def tariff_key_from_callback(data: str) -> str:
    key = data.replace("gift_r_", "").replace("r_", "")
    if "white" in key:
        key = key.replace("white_", "")
    if key.endswith("old"):
        key = key[:-3]
    return key


def panel_days_from_tariff_key(key: str) -> int:
    from config_bd.utils import _payload_duration_to_panel_days

    days = _payload_duration_to_panel_days(key)
    if days is not None:
        return days
    return int(key)
