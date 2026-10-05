"""Режим webhook для serverless (Vercel): три ASGI-приложения без сторонних фреймворков.

- webhook_app — Telegram присылает сюда каждое обновление (POST /api/webhook);
- tick_app    — проверки по расписанию: автораспределение, напоминания (GET /api/tick);
- setup_app   — один раз после деплоя: таблицы, webhook, меню команд (GET /api/setup);
- health_app  — самодиагностика с подсказками (GET /api/health).
"""

import asyncio
import hmac
import json
import logging
from typing import Any
from urllib.parse import parse_qs

from aiogram import Dispatcher
from aiogram.types import Update
from sqlalchemy import text
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


def _authorized(request: Request) -> bool:
    """Служебные адреса: ?secret=CRON_SECRET (или WEBHOOK_SECRET), либо заголовок от Vercel Cron."""
    given = request.query.get("secret", "") or request.headers.get("authorization", "").removeprefix("Bearer ").strip()
    return _same(given, config.cron_secret) or _same(given, config.webhook_secret)


def _current_base(request: Request) -> str:
    host = request.headers.get("x-forwarded-host") or request.headers.get("host", "")
    return config.public_url or f"https://{host}"


async def ensure_webhook(bot, base: str) -> bool:
    """Проверить, что Telegram шлёт обновления сюда, и поправить, если нет. True — если исправляли."""
    info = await bot.get_webhook_info()
    expected = f"{base}/api/webhook"
    # 401 в последней ошибке — секрет сменился, переустанавливаем с новым.
    if info.url == expected and "401" not in (info.last_error_message or ""):
        return False
    await bot.set_webhook(
        expected,
        secret_token=config.webhook_secret,
        allowed_updates=["message", "callback_query"],
        drop_pending_updates=False,
    )
    await set_commands(bot)
    log.info("Webhook set to %s", expected)
    return True


async def handle_tick(request: Request) -> tuple[int, Any]:
    if not _authorized(request):
        return 401, {"ok": False, "error": "bad secret"}

    sessionmaker, _ = await _ensure_ready()
    bot = make_bot()
    webhook_fixed = False
    try:
        try:
            webhook_fixed = await ensure_webhook(bot, _current_base(request))
        except Exception:
            log.exception("Webhook check failed")
        async with sessionmaker() as session:
            await tick(bot, session)
            await session.commit()
    finally:
        await bot.session.close()
    return 200, {"ok": True, "webhook_installed": webhook_fixed}


async def handle_setup(request: Request) -> tuple[int, Any]:
    if not _authorized(request):
        return 401, {"ok": False, "error": "передайте ?secret=<CRON_SECRET>"}
    if not config.bot_token:
        return 400, {"ok": False, "error": "не задана переменная BOT_TOKEN"}

    await _ensure_ready()
    base = _current_base(request)
    bot = make_bot()
    try:
        await bot.delete_webhook()  # принудительно переустанавливаем
        await ensure_webhook(bot, base)
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


async def handle_health(request: Request) -> tuple[int, Any]:
    """Самодиагностика: переменные, база, связь с Telegram. Webhook при необходимости чинит сам."""
    env = {
        "BOT_TOKEN": bool(config.bot_token),
        "ADMIN_IDS": bool(config.admin_ids),
        # Без DATABASE_URL бот взял бы локальный SQLite, который на Vercel не работает.
        "DATABASE_URL": not config.database_url.startswith("sqlite"),
        "CRON_SECRET": bool(config.cron_secret),
    }
    hints = [
        f"Не задана переменная {name}: Settings → Environment Variables, затем Deployments → Redeploy "
        "(без передеплоя новые переменные не подхватываются)."
        for name, ok in env.items()
        if not ok
    ]
    if not _authorized(request):
        if config.cron_secret:
            hints.append("Для полной проверки откройте этот адрес с ?secret=<CRON_SECRET>.")
        return 200, {"ok": not hints, "env": env, "hints": hints}

    report: dict[str, Any] = {"env": env}
    try:
        sessionmaker, _ = await _ensure_ready()
        async with sessionmaker() as session:
            await session.execute(text("select 1"))
        report["database"] = "ok"
    except Exception as e:  # noqa: BLE001 — показываем причину, без строки подключения
        report["database"] = f"error: {type(e).__name__}: {str(e)[:200]}"
        hints.append(
            "База недоступна: проверьте DATABASE_URL (строка из Neon целиком, с ?sslmode=require) "
            "и что проект в Neon не удалён."
        )

    bot = make_bot()
    try:
        me = await bot.me()
        fixed = await ensure_webhook(bot, _current_base(request))
        info = await bot.get_webhook_info()
        report["telegram"] = {
            "bot": f"@{me.username}",
            "webhook_url": info.url or None,
            "webhook_just_installed": fixed,
            "pending_updates": info.pending_update_count,
            "last_error": info.last_error_message,
        }
        error = info.last_error_message or ""
        if "401" in error:
            hints.append(
                "Telegram получает 401: на адрес стоит Deployment Protection. Откройте этот адрес с основного "
                "домена проекта или отключите Settings → Deployment Protection → Vercel Authentication."
            )
        elif error and not fixed:
            hints.append(f"Telegram сообщает об ошибке «{error}» — смотрите Vercel → Logs (функция api/webhook).")
    except Exception as e:  # noqa: BLE001
        report["telegram"] = f"error: {type(e).__name__}: {str(e)[:200]}"
        hints.append("Telegram не принимает BOT_TOKEN — проверьте токен у @BotFather.")
    finally:
        await bot.session.close()

    report["ok"] = not hints
    report["hints"] = hints or ["Всё в порядке. Напишите боту /start в личку."]
    return 200, report


webhook_app = _endpoint(handle_webhook)
health_app = _endpoint(handle_health)
tick_app = _endpoint(handle_tick)
setup_app = _endpoint(handle_setup)
