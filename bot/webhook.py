"""Режим webhook для serverless (Vercel): три ASGI-приложения без сторонних фреймворков.

- webhook_app — Telegram присылает сюда каждое обновление (POST /api/webhook);
- tick_app    — проверки по расписанию: автораспределение, напоминания (GET /api/tick);
- setup_app   — один раз после деплоя: таблицы, webhook, меню команд (GET /api/setup);
- health_app  — самодиагностика с подсказками (GET /api/health);
- app_page, miniapp_api_app — Telegram Mini App: страница (/app) и её API (POST /api/miniapp).
"""

import asyncio
import hmac
import json
import logging
import re
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

from aiogram import Dispatcher
from aiogram.types import MenuButtonWebApp, Update, WebAppInfo
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from bot import miniapp_api
from bot.app import make_bot, make_dispatcher, set_commands
from bot.config import config
from bot.miniapp_api import verify_init_data
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
        self.path = scope.get("path", "/")
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


class Html(str):
    """Ответ-страница вместо JSON."""


class Raw:
    """Файл (иконка, манифест) с типом и кэшированием."""

    def __init__(self, body: bytes, ctype: str, cache: str = "public, max-age=86400"):
        self.body, self.ctype, self.cache = body, ctype, cache


async def _respond(send, status: int, payload: Any) -> None:
    cache = b"no-store"
    if isinstance(payload, Raw):
        body, ctype, cache = payload.body, payload.ctype.encode(), payload.cache.encode()
    elif isinstance(payload, Html):
        body, ctype = payload.encode(), b"text/html; charset=utf-8"
    else:
        body, ctype = json.dumps(payload, ensure_ascii=False).encode(), b"application/json; charset=utf-8"
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [(b"content-type", ctype), (b"cache-control", cache)],
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


async def ensure_menu_button(bot, base: str) -> bool:
    """Кнопка «Открыть» рядом с полем ввода — открывает Mini App."""
    url = f"{base}/app"
    current = await bot.get_chat_menu_button()
    if isinstance(current, MenuButtonWebApp) and current.web_app.url == url:
        return False
    await bot.set_chat_menu_button(menu_button=MenuButtonWebApp(text="Открыть", web_app=WebAppInfo(url=url)))
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
            await ensure_menu_button(bot, _current_base(request))
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
        await ensure_menu_button(bot, base)
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
        await ensure_menu_button(bot, _current_base(request))
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


# ----------------------------------------------------------------- Mini App

_PAGE: str | None = None


def _page() -> str:
    global _PAGE
    if _PAGE is None:
        _PAGE = (Path(__file__).parent / "miniapp" / "index.html").read_text(encoding="utf-8")
    return _PAGE


ICONS = {"icon-192.png", "icon-512.png", "icon-180.png"}
_TEAM_NAME: str | None = None


async def _team_name() -> str:
    """Название для иконки на экране «Домой» — имя бота (один раз за запуск)."""
    global _TEAM_NAME
    if _TEAM_NAME is None:
        bot = make_bot()
        try:
            _TEAM_NAME = (await bot.me()).first_name or "Команда"
        except Exception:  # noqa: BLE001 — без Telegram тоже отдаём манифест
            return "Команда"
        finally:
            await bot.session.close()
    return _TEAM_NAME


async def handle_app_page(request: Request) -> tuple[int, Any]:
    asset = request.query.get("asset", "")
    if asset == "sw.js" or request.path.endswith("/sw.js"):
        raw = Raw((Path(__file__).parent / "miniapp" / "sw.js").read_bytes(), "application/javascript; charset=utf-8", "no-cache")
        return 200, raw
    if asset in ICONS:
        return 200, Raw((Path(__file__).parent / "miniapp" / asset).read_bytes(), "image/png")
    if "ics" in request.query:  # подписка на календарь (только главный админ)
        from bot import calendar_feed

        if calendar_feed.verify_feed_token(request.query.get("ics", "")) is None:
            return 404, {"ok": False, "error": "not found"}
        sessionmaker, _ = await _ensure_ready()
        async with sessionmaker() as session:
            body = await calendar_feed.build(session)
        return 200, Raw(body.encode(), "text/calendar; charset=utf-8", "no-cache, max-age=0")
    if "manifest" in request.query:
        # Установка на экран «Домой»: адрес запуска — с личным ключом, иначе установленное приложение
        # (у него своё хранилище) открылось бы без входа.
        key = request.query.get("k", "")
        start = "/app" + (f"?key={key}" if re.fullmatch(r"[0-9a-f.\-]{10,200}", key) else "")
        name = await _team_name()
        manifest = {
            "name": name, "short_name": name[:12], "start_url": start, "scope": "/", "display": "standalone",
            "background_color": "#0f1115", "theme_color": "#1f8a4c", "lang": "ru",
            "icons": [{"src": f"/api/app?asset=icon-{n}.png", "sizes": f"{n}x{n}", "type": "image/png", "purpose": "any"}
                      for n in (192, 512)],
        }
        return 200, Raw(json.dumps(manifest, ensure_ascii=False).encode(), "application/manifest+json", "no-store")
    return 200, Html(_page())


async def handle_miniapp_api(request: Request) -> tuple[int, Any]:
    if request.method != "POST":
        return 405, {"ok": False, "error": "POST only"}
    if b'"pin_login"' in (request.body or b""):  # вход по имени и PIN — без Telegram и без ссылки
        try:
            body = json.loads(request.body or b"{}")
        except ValueError:
            return 400, {"ok": False, "error": "Неверный запрос."}
        if body.get("action") == "pin_login":
            sessionmaker, _ = await _ensure_ready()
            async with sessionmaker() as session:
                try:
                    key = await miniapp_api.pin_login(session, str(body.get("name", "")), str(body.get("pin", "")))
                except miniapp_api.ApiError as e:
                    return e.status, {"ok": False, "error": str(e)}
                await session.commit()
            return 200, {"ok": True, "key": key}
    tg = verify_init_data(request.headers.get("x-telegram-init-data", ""), config.bot_token)
    if tg is None:  # браузер: вход по личной ссылке из бота
        tg = miniapp_api.verify_web_token(request.headers.get("x-web-token", ""), config.bot_token)
    if tg is None:
        return 401, {"ok": False, "error": "Откройте приложение из Telegram или по личной ссылке из бота (/web)."}
    try:
        body = json.loads(request.body or b"{}")
    except ValueError:
        return 400, {"ok": False, "error": "Неверный запрос."}

    sessionmaker, _ = await _ensure_ready()
    bot = make_bot()
    try:
        async with sessionmaker() as session:
            try:
                data = await miniapp_api.handle(bot, session, tg, body)
            except miniapp_api.ApiError as e:
                await session.rollback()
                return e.status, {"ok": False, "error": str(e)}
            await session.commit()
    finally:
        await bot.session.close()
    return 200, {"ok": True, **data}


webhook_app = _endpoint(handle_webhook)
app_page = _endpoint(handle_app_page)
miniapp_api_app = _endpoint(handle_miniapp_api)
health_app = _endpoint(handle_health)
tick_app = _endpoint(handle_tick)
setup_app = _endpoint(handle_setup)
