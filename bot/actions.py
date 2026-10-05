"""Сценарии, общие для хендлеров и планировщика."""

from aiogram import Bot
from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy.ext.asyncio import AsyncSession

from bot import keyboards, notifier, texts
from bot.models import Game, GameStatus, Rsvp, User
from bot.services import games as svc


async def distribute_and_announce(bot: Bot, session: AsyncSession, game: Game) -> str:
    """Распределить обязанности, обновить сообщения в чате и написать назначенным.

    Возвращает отчёт для администратора.
    """
    result = await svc.distribute_game(session, game)
    await notifier.refresh_game(bot, session, game)
    await notifier.announce_new_assignments(bot, game, result.assignments)

    yes = len((await svc.participants_by_status(session, game.id))[Rsvp.YES])
    lines = [f"🎯 Обязанности распределены: {texts.game_header(game)}", f"Участников: {yes}", ""]
    lines += texts.duties_block(result.assignments, result.unassigned)
    if result.unassigned:
        warning = texts.unassigned_warning(result.unassigned)
        lines += ["", warning]
        if game.chat_id and any(d.requires_car for d in result.unassigned):
            await bot.send_message(game.chat_id, texts.no_car_warning())
    return "\n".join(lines)


async def game_card(
    session: AsyncSession, game: Game, user: User | None, is_admin: bool
) -> tuple[str, InlineKeyboardMarkup | None]:
    """Карточка игры в личке: статус игрока, его обязанность, кнопки."""
    by_status = await svc.participants_by_status(session, game.id)
    lines = [f"<b>{texts.game_header(game)}</b>"]
    if game.location:
        lines.append(f"📍 {texts.h(game.location)}")
    lines.append(f"Подтвердили: {len(by_status[Rsvp.YES])}")

    my_duties = []
    if user is not None:
        status = await svc.get_rsvp(session, game.id, user.id)
        lines += ["", "Твой статус: " + (texts.RSVP_LABELS[status] if status else "не отмечен")]
        if game.status == GameStatus.DISTRIBUTED:
            my_duties = await svc.user_assignments(session, game.id, user.id)
            if my_duties:
                lines.append("Твоя обязанность: " + ", ".join(a.duty.title for a in my_duties))

    if game.status == GameStatus.DISTRIBUTED:
        assignments = await svc.active_assignments(session, game.id)
        unassigned = await svc.unassigned_duties(session, game)
        lines += ["", "<b>Обязанности</b>"] + texts.duties_block(assignments, unassigned)
    elif game.status == GameStatus.OPEN:
        lines += ["", "<i>Обязанности ещё не распределены.</i>"]

    b = InlineKeyboardBuilder()
    rsvp = keyboards.rsvp(game)
    if rsvp:
        b.row(*rsvp.inline_keyboard[0])
    if my_duties:
        b.button(text=texts.BTN_SWAP, callback_data=f"swap:{game.id}")
        b.adjust(3, 1)
    if is_admin:
        b.attach(InlineKeyboardBuilder.from_markup(keyboards.game_admin(game)))
    markup = b.as_markup()
    return "\n".join(lines), (markup if markup.inline_keyboard else None)
