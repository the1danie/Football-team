"""«🎒 Мячи/манишки у тебя, а ты не идёшь — кому передашь?» — кнопки в боте."""

from aiogram import Bot, F, Router
from aiogram.types import CallbackQuery
from sqlalchemy.ext.asyncio import AsyncSession

from bot import audit, gear
from bot.config import config
from bot.models import Duty, User
from bot.services import games as svc

router = Router()


@router.callback_query(F.data.regexp(r"^gh:\d+:\d+$"))
async def gear_pick(cb: CallbackQuery, session: AsyncSession):
    _, gid, did = cb.data.split(":")
    game = await svc.get_game(session, int(gid))
    me = await svc.get_user_by_tg(session, cb.from_user.id)
    h = next((x for x in await gear.holders(session, game) if x.duty.id == int(did)), None) if game else None
    if h is None or me is None:
        await cb.answer("Не нашёл — возможно, игра уже прошла.", show_alert=True)
        return
    await cb.answer()
    await cb.message.answer(gear.ask_text(game, h), reply_markup=gear.picker(game, h.duty, await gear.coming_players(session, game, h.user.id)))


@router.callback_query(F.data.regexp(r"^gh:\d+:\d+:\d+$"))
async def gear_give(cb: CallbackQuery, session: AsyncSession, bot: Bot):
    gid, did, uid = (int(x) for x in cb.data.split(":")[1:])
    game = await svc.get_game(session, gid)
    duty = await session.get(Duty, did)
    me = await svc.get_user_by_tg(session, cb.from_user.id)
    to_user = await session.get(User, uid) if uid else None
    if game is None or duty is None or me is None or (uid and to_user is None):
        await cb.answer("Не найдено.", show_alert=True)
        return
    try:
        note = await gear.handover(bot, session, game, duty, me, to_user, by_admin=config.is_admin(cb.from_user.id))
    except ValueError as e:
        await cb.answer(str(e), show_alert=True)
        return
    await audit.record(session, f"🎒 {duty.name}: передача" + (f" → {to_user.name}" if to_user else " (сам)"), game)
    await cb.answer()
    await cb.message.edit_text(note)
