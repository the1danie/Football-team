"""Личные ссылки для входа на сайт (веб-версия без Telegram)."""

import hashlib
import hmac
import time

from bot.config import config
from bot.models import User


WEB_TOKEN_DAYS = 180


def _web_key(bot_token: str) -> bytes:
    return hashlib.sha256(f"web-link:{bot_token}".encode()).digest()


def make_web_token(telegram_id: int, version: int, bot_token: str, days: int = WEB_TOKEN_DAYS) -> str:
    exp = int(time.time()) + days * 24 * 3600
    payload = f"{telegram_id}.{version}.{exp}"
    sig = hmac.new(_web_key(bot_token), payload.encode(), hashlib.sha256).hexdigest()[:32]
    return f"{payload}.{sig}"


def verify_web_token(token: str, bot_token: str) -> dict | None:
    """Личная ссылка для браузера. Возвращает {"id", "web_version"} или None."""
    try:
        tg_id, version, exp, sig = (token or "").split(".")
        payload = f"{tg_id}.{version}.{exp}"
        expected = hmac.new(_web_key(bot_token), payload.encode(), hashlib.sha256).hexdigest()[:32]
        if not bot_token or not hmac.compare_digest(expected, sig) or int(exp) < time.time():
            return None
        return {"id": int(tg_id), "web_version": int(version)}
    except ValueError:
        return None


def web_link(user: User) -> str | None:
    if not config.public_url:
        return None
    return f"{config.public_url}/app?key={make_web_token(user.telegram_id, user.web_version or 0, config.bot_token)}"




def _link_sig(user_id: int, bot_token: str) -> str:
    return hmac.new(_web_key(bot_token), f"tg-link:{user_id}".encode(), hashlib.sha256).hexdigest()[:12]


def link_payload(user: User) -> str:
    """Параметр для t.me/<бот>?start=… — привязать Telegram к игроку, добавленному вручную."""
    return f"link_{user.id}_{_link_sig(user.id, config.bot_token)}"


def parse_link_payload(payload: str) -> int | None:
    try:
        kind, raw_id, sig = (payload or "").split("_")
        if kind != "link" or not hmac.compare_digest(_link_sig(int(raw_id), config.bot_token), sig):
            return None
        return int(raw_id)
    except ValueError:
        return None
