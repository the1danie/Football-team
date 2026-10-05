"""Журнал действий админов.

Кто действует — кладём в contextvar на время обработки запроса (Telegram — в middleware,
приложение — в miniapp_api.handle). Действия бота по расписанию и самих игроков не пишем.
"""

import contextvars
import logging
from contextlib import contextmanager
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import config
from bot.models import AuditLog, Game

log = logging.getLogger(__name__)


@dataclass
class Actor:
    telegram_id: int
    user_id: int | None
    name: str


_actor: contextvars.ContextVar[Actor | None] = contextvars.ContextVar("audit_actor", default=None)


@contextmanager
def acting(telegram_id: int, user_id: int | None, name: str):
    token = _actor.set(Actor(telegram_id, user_id, (name or "?")[:64]))
    try:
        yield
    finally:
        _actor.reset(token)


def current() -> Actor | None:
    return _actor.get()


async def record(session: AsyncSession, text: str, game: Game | None = None) -> None:
    """Записать действие, если его делает админ (не бот по расписанию и не обычный игрок)."""
    actor = _actor.get()
    if actor is None or not config.is_admin(actor.telegram_id):
        return
    if game is not None:
        from bot import texts  # noqa: PLC0415

        text = f"{text} · {texts.game_header(game)}"
    session.add(AuditLog(actor_id=actor.user_id, actor_name=actor.name, text=text[:500],
                         game_id=game.id if game is not None else None))
