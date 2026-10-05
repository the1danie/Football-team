"""Сборка бота и диспетчера — общая для polling (сервер) и webhook (Vercel)."""

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import BotCommand, BotCommandScopeAllPrivateChats
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bot.config import config
from bot.fsm_storage import SqlStorage
from bot.handlers import build_router
from bot.middlewares import AccessMiddleware, DbSessionMiddleware

COMMANDS = [
    BotCommand(command="menu", description="Главное меню"),
    BotCommand(command="game", description="Текущая игра"),
    BotCommand(command="profile", description="Мой профиль"),
    BotCommand(command="stats", description="Статистика"),
    BotCommand(command="web", description="Открыть в браузере (личная ссылка)"),
    BotCommand(command="help", description="Помощь"),
]

_router = None


def make_bot() -> Bot:
    return Bot(config.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))


def make_dispatcher(sessionmaker: async_sessionmaker[AsyncSession]) -> Dispatcher:
    global _router
    if _router is None:  # роутеры aiogram — синглтоны, собираются один раз
        _router = build_router()
    # Роутер можно подключить только к одному диспетчеру; в проде он один, в тестах — новый на тест.
    _router._parent_router = None
    dp = Dispatcher(storage=SqlStorage(sessionmaker))
    dp.update.outer_middleware(DbSessionMiddleware(sessionmaker))
    dp.message.outer_middleware(AccessMiddleware())
    dp.callback_query.outer_middleware(AccessMiddleware())
    dp.include_router(_router)
    return dp


async def set_commands(bot: Bot) -> None:
    await bot.set_my_commands(COMMANDS, scope=BotCommandScopeAllPrivateChats())
