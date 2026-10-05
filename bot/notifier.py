"""Отправка и обновление сообщений в Telegram."""

import logging

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import InlineKeyboardMarkup
from sqlalchemy.ext.asyncio import AsyncSession

from bot import keyboards, texts
from bot.config import config
from bot.models import Game, GameStatus, Rsvp, User
from bot.services import games as svc

log = logging.getLogger(__name__)

GROUP_CHAT_KEY = "group_chat_id"


async def group_chat_id(session: AsyncSession) -> int | None:
    value = await svc.get_setting(session, GROUP_CHAT_KEY)
    if value:
        return int(value)
    return config.group_chat_id


async def send_dm(bot: Bot, user: User, text: str, reply_markup: InlineKeyboardMarkup | None = None) -> bool:
    """Личное сообщение. False — пользователь ещё не писал боту или заблокировал его."""
    try:
        await bot.send_message(user.telegram_id, text, reply_markup=reply_markup)
        return True
    except (TelegramForbiddenError, TelegramBadRequest) as e:
        log.info("DM to %s failed: %s", user.telegram_id, e)
        return False


async def notify_admins(bot: Bot, text: str) -> None:
    for admin_id in config.admin_ids:
        try:
            await bot.send_message(admin_id, text)
        except (TelegramForbiddenError, TelegramBadRequest) as e:
            log.info("Admin DM to %s failed: %s", admin_id, e)


async def _edit(bot: Bot, chat_id: int, message_id: int, text: str, markup) -> None:
    try:
        await bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=text, reply_markup=markup)
    except TelegramBadRequest as e:
        if "not modified" not in str(e):
            log.warning("Edit %s/%s failed: %s", chat_id, message_id, e)


async def publish_game(bot: Bot, session: AsyncSession, game: Game, chat_id: int) -> None:
    by_status = await svc.participants_by_status(session, game.id)
    msg = await bot.send_message(chat_id, texts.announce_text(game, by_status), reply_markup=keyboards.rsvp(game))
    game.chat_id = chat_id
    game.announce_message_id = msg.message_id
    await session.flush()


async def refresh_game(bot: Bot, session: AsyncSession, game: Game) -> None:
    """Обновить сообщение со сбором участников и сообщение с обязанностями."""
    if game.chat_id is None:
        return
    by_status = await svc.participants_by_status(session, game.id)
    if game.announce_message_id:
        await _edit(
            bot, game.chat_id, game.announce_message_id,
            texts.announce_text(game, by_status), keyboards.rsvp(game),
        )

    if game.status == GameStatus.OPEN:
        return
    if game.status == GameStatus.CANCELLED:
        assignments, unassigned = [], []
    else:
        assignments = await svc.active_assignments(session, game.id)
        unassigned = await svc.unassigned_duties(session, game)
    text = texts.summary_text(game, len(by_status[Rsvp.YES]), assignments, unassigned)
    me = await bot.me()
    markup = keyboards.swap_link(game, me.username)
    if game.summary_message_id:
        await _edit(bot, game.chat_id, game.summary_message_id, text, markup)
    elif game.status == GameStatus.DISTRIBUTED:
        msg = await bot.send_message(game.chat_id, text, reply_markup=markup)
        game.summary_message_id = msg.message_id
        await session.flush()


async def announce_reassignments(bot: Bot, game: Game, reassigned: list[svc.Reassignment]) -> None:
    for r in reassigned:
        await send_dm(
            bot, r.old_user,
            f"У тебя были назначены {r.duty.emoji} {texts.h(r.duty.name.lower())} ({texts.game_header(game)}).\n"
            "Обязанность будет передана другому игроку.",
        )
        if r.new_user is not None:
            await send_dm(
                bot, r.new_user,
                f"{r.old_user.name} не сможет прийти, поэтому тебе назначена обязанность:\n"
                f"{r.duty.emoji} {texts.h(r.duty.action)}\n\n{texts.game_header(game)}",
            )
        else:
            await notify_admins(
                bot,
                f"⚠️ {texts.game_header(game)}\n{texts.h(r.old_user.name)} отказался, "
                f"а замену для «{r.duty.title}» найти не удалось. Назначьте вручную.",
            )


async def announce_new_assignments(bot: Bot, game: Game, assignments) -> None:
    for a in assignments:
        await send_dm(
            bot, a.user,
            f"Тебе назначена обязанность на {texts.game_header(game)}:\n{a.duty.emoji} {texts.h(a.duty.action)}",
        )
