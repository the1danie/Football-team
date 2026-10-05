"""Запуск на сервере (polling): python -m bot"""

import asyncio
import logging

from bot.app import make_bot, make_dispatcher, set_commands
from bot.config import config
from bot.db import init_db, make_engine, make_sessionmaker
from bot.scheduler import run_scheduler


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if not config.bot_token:
        raise SystemExit("Не задан BOT_TOKEN (см. .env.example)")

    engine = make_engine(config.database_url)
    await init_db(engine)
    sessionmaker = make_sessionmaker(engine)

    bot = make_bot()
    dp = make_dispatcher(sessionmaker)
    await set_commands(bot)
    # Если бот раньше работал через webhook (Vercel), polling без этого не получит апдейты.
    await bot.delete_webhook()

    scheduler = asyncio.create_task(run_scheduler(bot, sessionmaker))
    try:
        await dp.start_polling(bot)
    finally:
        scheduler.cancel()
        await bot.session.close()
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
