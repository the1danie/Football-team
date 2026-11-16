"""Кнопки в боте — как в приложении: «⏰ Опаздываю», «🧮 Почему так», админу — «⏰ Опоздание»."""

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot import discipline, explain, keyboards, texts
from bot.config import config
from bot.models import Game, GameParticipant, GameStatus, Rsvp, User
from bot.services import games as svc

router = Router()


class LateReason(StatesGroup):
    reason = State()


async def _me(session: AsyncSession, tg_id: int) -> User | None:
    return await svc.get_user_by_tg(session, tg_id)


async def _late_games(session: AsyncSession, user: User) -> list[Game]:
    """Игры, где можно нажать «Опаздываю»: сегодня, «Буду», сбор ещё актуален."""
    out = []
    for g in await svc.upcoming_games(session, config.now(), grace_hours=1):
        if g.status in GameStatus.ACTIVE and discipline.late_notice_open(g) \
                and await svc.get_rsvp(session, g.id, user.id) == Rsvp.YES:
            out.append(g)
    return out


# ----------------------------------------------------------------- игрок: «⏰ Опаздываю»


@router.message(F.chat.type == "private", F.text == texts.BTN_LATE)
async def late_menu(message: Message, session: AsyncSession, state: FSMContext):
    await state.clear()
    user = await _me(session, message.from_user.id)
    games = await _late_games(session, user) if user else []
    if not games:
        await message.answer("⏰ «Опаздываю» можно нажать в день игры, если ты отметил «✅ Буду».")
        return
    for g in games:
        await _ask_minutes(message, session, g, user)


async def _ask_minutes(message: Message, session: AsyncSession, game: Game, user: User, edit: bool = False):
    p = await session.get(GameParticipant, (game.id, user.id))
    now = f"\nСейчас: предупредил ~{p.late_notice_minutes} мин." if p and p.late_notice_minutes else ""
    text = f"⏰ <b>{texts.game_header(game)}</b>\nПримерно на сколько опоздаешь?{now}"
    markup = keyboards.late_notice_choice(game.id, bool(p and p.late_notice_minutes))
    if edit:
        await message.edit_text(text, reply_markup=markup)
    else:
        await message.answer(text, reply_markup=markup)


@router.callback_query(F.data.regexp(r"^lt:\d+$"))
async def late_pick(cb: CallbackQuery, session: AsyncSession):
    game = await svc.get_game(session, int(cb.data.split(":")[1]))
    user = await _me(session, cb.from_user.id)
    if game is None or user is None:
        await cb.answer("Игра не найдена.", show_alert=True)
        return
    await cb.answer()
    await _ask_minutes(cb.message, session, game, user)


@router.callback_query(F.data.regexp(r"^lt:\d+:\d+$"))
async def late_minutes(cb: CallbackQuery, session: AsyncSession, state: FSMContext, bot: Bot):
    _, gid, minutes = cb.data.split(":")
    game = await svc.get_game(session, int(gid))
    user = await _me(session, cb.from_user.id)
    if game is None or user is None:
        await cb.answer("Игра не найдена.", show_alert=True)
        return
    if minutes == "0":  # успеваю
        try:
            note = await discipline.late_notice(bot, session, game, user, 0, None)
        except ValueError as e:
            await cb.answer(str(e), show_alert=True)
            return
        await cb.message.edit_text(f"✅ {note}")
        await cb.answer()
        return
    await state.set_state(LateReason.reason)
    await state.update_data(late_game=int(gid), late_minutes=int(minutes))
    await cb.message.edit_text(
        f"⏰ ~{minutes} мин — {texts.game_header(game)}\nНапиши причину одним сообщением (пробки, работа…) "
        "— админ увидит её и при уважительной причине минус не поставит.",
        reply_markup=keyboards.late_reason_skip(game.id, int(minutes)))
    await cb.answer()


async def _send_notice(bot: Bot, session: AsyncSession, tg_id: int, game_id: int, minutes: int, reason: str | None) -> str:
    game = await svc.get_game(session, game_id)
    user = await _me(session, tg_id)
    if game is None or user is None:
        return "Игра не найдена."
    try:
        return await discipline.late_notice(bot, session, game, user, minutes, reason)
    except ValueError as e:
        return str(e)


@router.message(LateReason.reason, F.chat.type == "private", F.text, ~F.text.startswith("/"))
async def late_reason(message: Message, session: AsyncSession, state: FSMContext, bot: Bot):
    from bot.handlers.common import MENU_BUTTONS  # noqa: PLC0415

    data = await state.get_data()
    await state.clear()
    if message.text in MENU_BUTTONS:  # нажал кнопку меню — без причины не отправляем, просто выходим
        await message.answer("Предупреждение не отправлено. Нажми «⏰ Опаздываю» ещё раз.")
        return
    await message.answer(await _send_notice(bot, session, message.from_user.id, data["late_game"],
                                            data["late_minutes"], message.text))


@router.callback_query(F.data.startswith("ltr:"))
async def late_no_reason(cb: CallbackQuery, session: AsyncSession, state: FSMContext, bot: Bot):
    _, gid, minutes = cb.data.split(":")
    await state.clear()
    note = await _send_notice(bot, session, cb.from_user.id, int(gid), int(minutes), None)
    await cb.message.edit_text(note)
    await cb.answer()


# ----------------------------------------------------------------- «🧮 Почему так»


@router.callback_query(F.data.startswith("why:"))
async def why(cb: CallbackQuery, session: AsyncSession):
    game = await svc.get_game(session, int(cb.data.split(":")[1]))
    user = await _me(session, cb.from_user.id)
    data = await explain.explain(session, game) if game is not None else None
    if data is None or user is None:
        await cb.answer("На эту игру обязанности ещё не распределялись.", show_alert=True)
        return
    await cb.answer()
    await cb.message.answer(explain.personal_text(data, user.id))


# ----------------------------------------------------------------- админ: «⏰ Опоздание»


def _tag(p: GameParticipant) -> str:
    if p.late_minutes:
        return f" ⏰{p.late_minutes}" + (" ✔" if p.late_excused else "")
    if p.late_notice_minutes:
        return f" ⏰~{p.late_notice_minutes}"
    return ""


async def _players_markup(session: AsyncSession, game: Game):
    parts = (await session.scalars(select(GameParticipant).where(
        GameParticipant.game_id == game.id, GameParticipant.status == Rsvp.YES,
        GameParticipant.attended.is_not(False)))).unique().all()
    rows = sorted(((p.user_id, p.user.name, _tag(p)) for p in parts), key=lambda x: x[1].lower())
    return keyboards.late_admin_players(game.id, rows)


@router.callback_query(F.data.regexp(r"^lta:\d+(:\d+)?$"))
async def late_admin(cb: CallbackQuery, session: AsyncSession):
    if not config.is_admin(cb.from_user.id):
        await cb.answer("Только для админа.", show_alert=True)
        return
    ids = [int(x) for x in cb.data.split(":")[1:]]
    game = await svc.get_game(session, ids[0])
    if game is None:
        await cb.answer("Игра не найдена.", show_alert=True)
        return
    await cb.answer()
    if len(ids) == 1:
        await cb.message.edit_text(
            f"⏰ <b>Кто опоздал — {texts.game_header(game)}</b>\n"
            "Опоздал на 10+ минут от начала — −1. ⏰~15 — сам предупредил.",
            reply_markup=await _players_markup(session, game))
        return
    p = await session.get(GameParticipant, (game.id, ids[1]))
    if p is None:
        return
    info = []
    if p.late_notice_minutes:
        info.append(f"Предупредил: ~{p.late_notice_minutes} мин" + (f" — «{texts.h(p.late_notice_reason)}»" if p.late_notice_reason else ""))
    if p.late_minutes:
        info.append(f"Отмечено: {p.late_minutes} мин" + (" (форс-мажор)" if p.late_excused else ""))
    await cb.message.edit_text(
        f"⏰ <b>{texts.h(p.user.name)}</b> — на сколько опоздал?\n" + "\n".join(info),
        reply_markup=keyboards.late_admin_minutes(game.id, p.user_id, bool(p.late_minutes), p.late_notice_minutes))


@router.callback_query(F.data.regexp(r"^lta:\d+:\d+:\d+:[01]$"))
async def late_admin_set(cb: CallbackQuery, session: AsyncSession, bot: Bot):
    if not config.is_admin(cb.from_user.id):
        await cb.answer("Только для админа.", show_alert=True)
        return
    gid, uid, minutes, excused = (int(x) for x in cb.data.split(":")[1:])
    game = await svc.get_game(session, gid)
    target = await session.get(User, uid)
    if game is None or target is None:
        await cb.answer("Не найдено.", show_alert=True)
        return
    try:
        await discipline.check_self_late(session, await _me(session, cb.from_user.id), target, game, minutes, bool(excused))
        note = await discipline.set_late(bot, session, game, target, minutes, bool(excused))
    except ValueError as e:
        await cb.answer(str(e), show_alert=True)
        return
    await cb.answer(note[:190])
    await cb.message.edit_text(
        f"⏰ <b>Кто опоздал — {texts.game_header(game)}</b>\n{texts.h(note)}",
        reply_markup=await _players_markup(session, game))
