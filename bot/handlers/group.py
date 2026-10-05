from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot import actions, notifier, operations, texts
from bot.config import config
from bot.middlewares import IsAdmin
from bot.models import Rsvp
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
    if game is None:
        await cb.answer("Игра не найдена.", show_alert=True)
        return
    user = await svc.get_user_by_tg(session, cb.from_user.id)  # подтверждённый — проверил AccessMiddleware
    if user is None:
        await cb.answer("Сначала нажми /start в личке с ботом.", show_alert=True)
        return
    had_duties = await svc.user_assignments(session, game.id, user.id)
    try:
        result = await operations.change_rsvp(bot, session, game, user, status)
    except operations.OpError as e:
        await cb.answer(str(e), show_alert=True)
        return
    if not result.changed:
        await cb.answer(f"Ты уже отметил: {texts.RSVP_LABELS[status]}")
        return

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
