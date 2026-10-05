from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy.ext.asyncio import AsyncSession

from bot import texts
from bot.models import User
from bot.services import games as svc

router = Router()


def duties_word(n: int) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return "обязанность"
    if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14):
        return "обязанности"
    return "обязанностей"


async def team_view(session: AsyncSession):
    rows = await svc.team_stats(session)
    if not rows:
        return "Статистики пока нет.", None
    lines = ["<b>📊 Статистика команды</b>", ""]
    lines += [f"{texts.h(u.name)} — {n} {duties_word(n)}" for u, n in rows]
    lines += ["", "Нажми на игрока, чтобы посмотреть подробности."]
    b = InlineKeyboardBuilder()
    for u, _ in rows:
        b.button(text=u.name, callback_data=f"stat:{u.id}")
    b.adjust(3)
    return "\n".join(lines), b.as_markup()


@router.message(F.chat.type == "private", F.text == texts.BTN_STATS)
@router.message(Command("stats"))
async def stats(message: Message, session: AsyncSession, state: FSMContext):
    await state.clear()
    text, markup = await team_view(session)
    await message.answer(text, reply_markup=markup)


@router.callback_query(F.data == "stat:all")
async def stats_back(cb: CallbackQuery, session: AsyncSession):
    text, markup = await team_view(session)
    await cb.message.edit_text(text, reply_markup=markup)
    await cb.answer()


@router.callback_query(F.data.startswith("stat:"))
async def player(cb: CallbackQuery, session: AsyncSession):
    user = await session.get(User, int(cb.data.split(":")[1]))
    await cb.answer()
    if user is None:
        return
    lines = [f"<b>{texts.h(user.name)}</b>{' 🚗' if user.has_car else ''}", ""]
    lines += [f"{d.emoji} {texts.h(d.name)} — {n}" for d, n in await svc.player_stats(session, user.id)]
    b = InlineKeyboardBuilder()
    b.button(text="← Вся команда", callback_data="stat:all")
    await cb.message.edit_text("\n".join(lines), reply_markup=b.as_markup())
