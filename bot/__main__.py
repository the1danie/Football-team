import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import BotCommand, BotCommandScopeAllPrivateChats

from bot.config import config
from bot.db import init_db, make_engine, make_sessionmaker
from bot.handlers import build_router
from bot.middlewares import DbSessionMiddleware
from bot.scheduler import run_scheduler


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if not config.bot_token:
        raise SystemExit("Не задан BOT_TOKEN (см. .env.example)")

    engine = make_engine(config.database_url)
    await init_db(engine)
    sessionmaker = make_sessionmaker(engine)

    bot = Bot(config.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.update.outer_middleware(DbSessionMiddleware(sessionmaker))
    dp.include_router(build_router())

    await bot.set_my_commands(
        [
            BotCommand(command="menu", description="Главное меню"),
            BotCommand(command="game", description="Текущая игра"),
            BotCommand(command="profile", description="Мой профиль"),
            BotCommand(command="stats", description="Статистика"),
            BotCommand(command="help", description="Помощь"),
        ],
        scope=BotCommandScopeAllPrivateChats(),
    )

    scheduler = asyncio.create_task(run_scheduler(bot, sessionmaker))
    try:
        await dp.start_polling(bot)
    finally:
        scheduler.cancel()
        await bot.session.close()
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
