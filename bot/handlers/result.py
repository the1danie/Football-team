"""Результат игры в боте: админ — «✍️ Ввести счёт», игрок — «⚽ +1 гол», «🅰️ +1 пас»."""

import re

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot import audit, results, texts, whatsapp
from bot.config import config
from bot.models import GameStat
from bot.services import games as svc

router = Router()


class ResultScore(StatesGroup):
    score = State()


@router.callback_query(F.data.regexp(r"^rs:\d+:[ga0]$"))
async def my_stat(cb: CallbackQuery, session: AsyncSession):
    _, gid, what = cb.data.split(":")
    game = await svc.get_game(session, int(gid))
    user = await svc.get_user_by_tg(session, cb.from_user.id)
    if game is None or user is None:
        await cb.answer("Игра не найдена.", show_alert=True)
        return
    row = await session.scalar(select(GameStat).where(GameStat.game_id == game.id, GameStat.user_id == user.id))
    goals, assists = (row.goals, row.assists) if row else (0, 0)
    if what == "g":
        goals += 1
    elif what == "a":
        assists += 1
    else:
        goals = assists = 0
    try:
        await results.set_stat(session, game, user, goals, assists, by_admin=False)
    except results.ResultError as e:
        await cb.answer(str(e)[:190], show_alert=True)
        return
    await cb.answer("Записано")
    await cb.message.edit_text(
        f"⚽ <b>{texts.game_header(game)}</b>\nТвои: ⚽ {goals} · 🅰️ {assists}. Ошибся — «↩️ Сбросить мои».",
        reply_markup=cb.message.reply_markup)


@router.callback_query(F.data.regexp(r"^res:\d+$"))
async def ask_score(cb: CallbackQuery, state: FSMContext):
    if not config.is_admin(cb.from_user.id):
        await cb.answer("Счёт вносит админ.", show_alert=True)
        return
    await state.set_state(ResultScore.score)
    await state.update_data(result_game=int(cb.data.split(":")[1]))
    await cb.answer()
    await cb.message.answer("Пришлите счёт: наши голы и голы соперника, например 5:3")


@router.message(ResultScore.score, F.chat.type == "private", F.text)
async def got_score(message: Message, session: AsyncSession, state: FSMContext, bot: Bot):
    from bot.handlers.common import MENU_BUTTONS  # noqa: PLC0415

    if message.text in MENU_BUTTONS or message.text.startswith("/"):
        await state.clear()
        await message.answer("Ввод счёта отменён. Нажмите «✍️ Ввести счёт» ещё раз, когда будете готовы.")
        return
    m = re.fullmatch(r"\s*(\d{1,2})\s*[:\-–—]\s*(\d{1,2})\s*", message.text or "")
    if m is None:
        await message.answer("Не понял. Напишите так: 5:3 (сначала наши голы).")
        return
    data = await state.get_data()
    await state.clear()
    game = await svc.get_game(session, data.get("result_game", 0))
    if game is None:
        await message.answer("Игра не найдена.")
        return
    try:
        await results.set_score(session, game, m.group(1), m.group(2))
    except results.ResultError as e:
        await message.answer(str(e))
        return
    await audit.record(session, f"🏁 Счёт {game.score_us}:{game.score_them}", game)
    await message.answer(f"✅ {texts.game_header(game)}: {game.score_us}:{game.score_them} — {results.outcome(game)}. "
                         "Голы и пасы игроки отмечают сами — итог для WhatsApp ниже 👇")
    await whatsapp.send_draft(bot, await results.whatsapp_text(session, game), chat_ids=[message.chat.id])
