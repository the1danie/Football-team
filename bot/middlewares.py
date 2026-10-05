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


ONBOARDING_COMMANDS = ("/start", "/help")


class AccessMiddleware(BaseMiddleware):
    """В команду — только через подтверждение админом.

    Пока игрок не подтверждён, ему доступна только регистрация (/start и выбор машины).
    Заблокированным — ничего.
    """

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        from bot.models import UserStatus  # noqa: PLC0415 — избегаем циклических импортов
        from bot.services import games as svc  # noqa: PLC0415

        tg_user = data.get("event_from_user")
        if tg_user is None:
            return await handler(event, data)
        session = data["session"]
        user = await svc.get_user_by_tg(session, tg_user.id)
        if user is not None and user.username != tg_user.username:
            user.username = tg_user.username
        if config.is_admin(tg_user.id):
            if user is not None and user.status != UserStatus.APPROVED:
                user.status = UserStatus.APPROVED
            return await handler(event, data)
        if user is not None and user.status == UserStatus.APPROVED:
            return await handler(event, data)

        if user is not None and user.status == UserStatus.BLOCKED:
            await self._deny(event, "⛔ Доступ к боту закрыт администратором.")
            return None
        if self._is_onboarding(event, data):
            return await handler(event, data)
        if user is not None and user.profile_completed:
            text = "⏳ Заявка у администратора. Как только подтвердит — придёт сообщение, и можно будет отмечаться."
        else:
            text = "Сначала нажми /start в личке с ботом — администратор подтвердит, что ты из команды."
        await self._deny(event, text)
        return None

    @staticmethod
    def _is_onboarding(event: TelegramObject, data: dict[str, Any]) -> bool:
        raw_state = data.get("raw_state") or ""
        if isinstance(event, Message):
            if event.chat.type != "private":
                return False
            text = event.text or ""
            return text.split(" ")[0].split("@")[0] in ONBOARDING_COMMANDS or raw_state.startswith("ProfileStates")
        if isinstance(event, CallbackQuery):
            return (event.data or "").startswith("car:") and raw_state.startswith("ProfileStates")
        return False

    @staticmethod
    async def _deny(event: TelegramObject, text: str) -> None:
        if isinstance(event, CallbackQuery):
            await event.answer(text, show_alert=True)
        elif isinstance(event, Message) and event.chat.type == "private":
            await event.answer(text)
