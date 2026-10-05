from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.filters import BaseFilter
from aiogram.types import CallbackQuery, Message, TelegramObject
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bot.config import config
from bot.fsm_storage import current_session


class DbSessionMiddleware(BaseMiddleware):
    """Открывает сессию БД на каждый апдейт и коммитит её после обработки."""

    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession]):
        self.sessionmaker = sessionmaker

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        async with self.sessionmaker() as session:
            data["session"] = session
            token = current_session.set(session)
            try:
                result = await handler(event, data)
            except Exception:
                await session.rollback()
                raise
            finally:
                current_session.reset(token)
            await session.commit()
            return result


class IsAdmin(BaseFilter):
    async def __call__(self, event: Message | CallbackQuery) -> bool:
        return event.from_user is not None and config.is_admin(event.from_user.id)
