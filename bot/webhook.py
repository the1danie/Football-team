"""Режим webhook для serverless (Vercel): три ASGI-приложения без сторонних фреймворков.

- webhook_app — Telegram присылает сюда каждое обновление (POST /api/webhook);
- tick_app    — проверки по расписанию: автораспределение, напоминания (GET /api/tick);
- setup_app   — один раз после деплоя: таблицы, webhook, меню команд (GET /api/setup).
"""

import asyncio
import hmac
import json
import logging
from typing import Any
from urllib.parse import parse_qs

from aiogram import Dispatcher
from aiogram.types import Update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from bot.app import make_bot, make_dispatcher, set_commands
from bot.config import config
from bot.db import init_db, make_engine, make_sessionmaker
from bot.scheduler import tick

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

# Живут, пока жив экземпляр функции (между «тёплыми» вызовами).
_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None
_dp: Dispatcher | None = None
_db_ready = False
_init_lock = asyncio.Lock()


async def _ensure_ready() -> tuple[async_sessionmaker[AsyncSession], Dispatcher]:
    global _engine, _sessionmaker, _dp, _db_ready
    async with _init_lock:
        if _engine is None:
            _engine = make_engine(config.database_url, serverless=True)
            _sessionmaker = make_sessionmaker(_engine)
            _dp = make_dispatcher(_sessionmaker)
        if not _db_ready:
            await init_db(_engine)  # create_all идемпотентен; на холодном старте — пара запросов
            _db_ready = True
    return _sessionmaker, _dp


# ----------------------------------------------------------------- ASGI-обвязка


class Request:
    def __init__(self, scope: dict, body: bytes):
        self.method = scope.get("method", "GET")
        self.headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
        self.query = {k: v[0] for k, v in parse_qs(scope.get("query_string", b"").decode()).items()}
        self.body = body


async def _read(scope: dict, receive) -> Request:
    body = b""
    while True:
        message = await receive()
        body += message.get("body", b"")
        if not message.get("more_body"):
            break
    return Request(scope, body)


async def _respond(send, status: int, payload: Any) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode()
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [(b"content-type", b"application/json; charset=utf-8")],
        }
    )
    await send({"type": "http.response.body", "body": body})


async def _lifespan(receive, send) -> None:
    while True:
        message = await receive()
        if message["type"] == "lifespan.startup":
            await send({"type": "lifespan.startup.complete"})
        elif message["type"] == "lifespan.shutdown":
            await send({"type": "lifespan.shutdown.complete"})
            return


def _same(a: str, b: str) -> bool:
    return bool(a) and bool(b) and hmac.compare_digest(a.encode(), b.encode())


def _endpoint(handler):
    async def app(scope, receive, send):
        if scope["type"] == "lifespan":
            await _lifespan(receive, send)
            return
        if scope["type"] != "http":
            return
        request = await _read(scope, receive)
        try:
            status, payload = await handler(request)
        except Exception:
            log.exception("Request failed")
            status, payload = 500, {"ok": False, "error": "internal error"}
        await _respond(send, status, payload)

    return app


# ----------------------------------------------------------------- обработчики


async def handle_webhook(request: Request) -> tuple[int, Any]:
    if request.method != "POST":
        return 200, {"ok": True, "hint": "Telegram webhook endpoint"}
    if not _same(request.headers.get("x-telegram-bot-api-secret-token", ""), config.webhook_secret):
        return 401, {"ok": False, "error": "bad secret"}

    _, dp = await _ensure_ready()
    bot = make_bot()
    try:
        update = Update.model_validate(json.loads(request.body), context={"bot": bot})
        await dp.feed_update(bot, update)
    except Exception:
        # Отвечаем 200, иначе Telegram будет бесконечно повторять «сломанный» апдейт.
        log.exception("Update processing failed")
    finally:
        await bot.session.close()
    return 200, {"ok": True}


async def handle_tick(request: Request) -> tuple[int, Any]:
    auth = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
    if not (_same(auth, config.cron_secret) or _same(request.query.get("secret", ""), config.cron_secret)):
        return 401, {"ok": False, "error": "bad secret"}

    sessionmaker, _ = await _ensure_ready()
    bot = make_bot()
    try:
        async with sessionmaker() as session:
            await tick(bot, session)
            await session.commit()
    finally:
        await bot.session.close()
    return 200, {"ok": True}


async def handle_setup(request: Request) -> tuple[int, Any]:
    if not _same(request.query.get("secret", ""), config.webhook_secret):
        return 401, {"ok": False, "error": "передайте ?secret=<WEBHOOK_SECRET>"}
    missing = [n for n, v in (("BOT_TOKEN", config.bot_token), ("CRON_SECRET", config.cron_secret)) if not v]
    if missing:
        return 400, {"ok": False, "error": "не заданы переменные: " + ", ".join(missing)}

    await _ensure_ready()
    base = config.public_url or f"https://{request.headers.get('x-forwarded-host') or request.headers['host']}"
    url = f"{base}/api/webhook"
    bot = make_bot()
    try:
        await bot.set_webhook(
            url,
            secret_token=config.webhook_secret,
            allowed_updates=["message", "callback_query"],
            drop_pending_updates=False,
        )
        await set_commands(bot)
        me = await bot.me()
        info = await bot.get_webhook_info()
    finally:
        await bot.session.close()
    return 200, {
        "ok": True,
        "bot": f"@{me.username}",
        "webhook": info.url,
        "pending_updates": info.pending_update_count,
        "tick_url": f"{base}/api/tick?secret=<CRON_SECRET>",
    }


webhook_app = _endpoint(handle_webhook)
tick_app = _endpoint(handle_tick)
setup_app = _endpoint(handle_setup)
