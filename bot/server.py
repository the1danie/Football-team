"""Запуск на своём сервере (VPS): одно приложение вместо функций Vercel.

    uvicorn bot.server:app --host 127.0.0.1 --port 8000

Отвечает на те же адреса, что и Vercel (/api/webhook, /api/miniapp, /app, …), а планировщик
(напоминания, распределение, отчёты) крутится внутри — cron-job.org не нужен. Перед ним — Caddy с HTTPS.
"""

import asyncio
import logging

from bot import webhook
from bot.config import config
from bot.scheduler import tick

log = logging.getLogger(__name__)

TICK_SECONDS = 60
WEBHOOK_CHECK_EVERY = 10  # раз в 10 тиков проверить, что Telegram шлёт обновления сюда

ROUTES = {
    "/api/webhook": webhook.webhook_app,
    "/api/miniapp": webhook.miniapp_api_app,
    "/api/health": webhook.health_app,
    "/api/tick": webhook.tick_app,
    "/api/setup": webhook.setup_app,
    "/api/app": webhook.app_page,
    "/app": webhook.app_page,
    "/sw.js": webhook.app_page,
    "/": webhook.app_page,
}


async def _check_webhook(bot) -> None:
    if not config.manage_webhook:
        return
    if not config.public_url:
        log.warning("PUBLIC_URL не задан — webhook не проверяю")
        return
    try:
        await webhook.ensure_webhook(bot, config.public_url)
        await webhook.ensure_menu_button(bot, config.public_url)
    except Exception:
        log.exception("Webhook check failed")


async def scheduler_loop() -> None:
    sessionmaker, _ = await webhook._ensure_ready()
    bot = webhook.make_bot()
    n = 0
    try:
        while True:
            if n % WEBHOOK_CHECK_EVERY == 0:
                await _check_webhook(bot)
            try:
                async with sessionmaker() as session:
                    await tick(bot, session)
                    await session.commit()
            except Exception:
                log.exception("Scheduler tick failed")
            n += 1
            await asyncio.sleep(TICK_SECONDS)
    finally:
        await bot.session.close()


async def _lifespan(receive, send) -> None:
    task = None
    while True:
        message = await receive()
        if message["type"] == "lifespan.startup":
            try:
                await webhook._ensure_ready()  # таблицы и стандартные обязанности
                if config.run_scheduler:
                    task = asyncio.create_task(scheduler_loop())
            except Exception as e:  # noqa: BLE001
                log.exception("Startup failed")
                await send({"type": "lifespan.startup.failed", "message": str(e)})
                return
            await send({"type": "lifespan.startup.complete"})
        elif message["type"] == "lifespan.shutdown":
            if task:
                task.cancel()
            await send({"type": "lifespan.shutdown.complete"})
            return


async def _not_found(send) -> None:
    await send({"type": "http.response.start", "status": 404, "headers": [(b"content-type", b"text/plain; charset=utf-8")]})
    await send({"type": "http.response.body", "body": "Не найдено".encode()})


async def app(scope, receive, send):
    if scope["type"] == "lifespan":
        await _lifespan(receive, send)
        return
    if scope["type"] != "http":
        return
    path = scope.get("path", "/").rstrip("/") or "/"
    handler = ROUTES.get(path)
    if handler is None:
        await _not_found(send)
        return
    await handler(scope, receive, send)
