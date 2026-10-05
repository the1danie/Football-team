from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot import actions, notifier, texts
from bot.config import config
from bot.middlewares import IsAdmin
from bot.models import GameStatus, Rsvp
from bot.services import games as svc

router = Router()


@router.message(Command("bindchat"), F.chat.type.in_({"group", "supergroup"}), IsAdmin())
async def bind_chat(message: Message, session: AsyncSession):
    await svc.set_setting(session, notifier.GROUP_CHAT_KEY, str(message.chat.id))
    await message.answer("✅ Этот чат привязан как чат команды. Сюда будут публиковаться игры.")


@router.message(Command("bindchat"), F.chat.type.in_({"group", "supergroup"}))
async def bind_chat_denied(message: Message):
    await message.reply("Эта команда только для администраторов бота.")


@router.callback_query(F.data.startswith("rsvp:"))
async def on_rsvp(cb: CallbackQuery, session: AsyncSession, bot: Bot):
    _, game_id, status = cb.data.split(":")
    if status not in Rsvp.ALL:
        await cb.answer()
        return
    game = await svc.get_game(session, int(game_id))
    if game is None or game.status not in GameStatus.ACTIVE:
        await cb.answer("Сбор по этой игре закрыт.", show_alert=True)
        return
    if config.now() >= game.starts_at:
        await cb.answer("Игра уже началась.", show_alert=True)
        return

    user = await svc.get_or_create_user(session, cb.from_user.id, cb.from_user.first_name)
    had_duties = await svc.user_assignments(session, game.id, user.id)
    result = await svc.set_rsvp(session, game, user, status)
    if not result.changed:
        await cb.answer(f"Ты уже отметил: {texts.RSVP_LABELS[status]}")
        return

    await notifier.refresh_game(bot, session, game)
    await notifier.announce_reassignments(bot, game, result.reassigned)
    await notifier.announce_new_assignments(bot, game, result.filled)

    if cb.message and cb.message.chat.type == "private":
        text, markup = await actions.game_card(session, game, user, config.is_admin(cb.from_user.id))
        await cb.message.edit_text(text, reply_markup=markup)

    alert = f"Отмечено: {texts.RSVP_LABELS[status]}"
    show_alert = False
    if result.reassigned and had_duties:
        duties = ", ".join(f"{r.duty.emoji} {r.duty.name.lower()}" for r in result.reassigned)
        alert = f"У тебя были назначены {duties}.\nОбязанность будет передана другому игроку."
        show_alert = True
    elif not user.profile_completed:
        me = await bot.me()
        alert += (
            f"\n\nЗаполни профиль (имя и есть ли машина) — напиши боту @{me.username} в личку."
        )
        show_alert = True
    await cb.answer(alert, show_alert=show_alert)
