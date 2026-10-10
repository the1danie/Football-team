from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy.ext.asyncio import AsyncSession

from bot import notifier, operations, texts
from bot.config import config
from bot.models import Assignment, AssignmentStatus, GameStatus, SwapRequest, SwapStatus, User
from bot.services import games as svc

router = Router()


async def open_swap(message: Message, session: AsyncSession, user: User, game_id: int) -> None:
    """Начать обмен: показать свою обязанность и список участников."""
    game = await svc.get_game(session, game_id)
    if game is None or game.status != GameStatus.DISTRIBUTED:
        await message.answer("По этой игре обмен недоступен.")
        return
    mine = await svc.user_assignments(session, game.id, user.id)
    if not mine:
        await message.answer(f"На {texts.game_header(game)} у тебя нет обязанностей.")
        return
    if len(mine) > 1:
        b = InlineKeyboardBuilder()
        for a in mine:
            b.button(text=a.duty.title, callback_data=f"swapmine:{a.id}")
        b.adjust(1)
        await message.answer("Какую обязанность хочешь поменять?", reply_markup=b.as_markup())
        return
    await show_targets(message, session, mine[0])


async def show_targets(message: Message, session: AsyncSession, assignment: Assignment) -> None:
    game = await svc.get_game(session, assignment.game_id)
    targets = await svc.swap_targets(session, game, assignment)
    text = f"{texts.game_header(game)}\n\nТвоя обязанность: <b>{assignment.duty.title}</b>"
    if not targets:
        hint = " с машиной" if assignment.duty.requires_car else ""
        await message.answer(f"{text}\n\nНет других участников{hint}, с кем можно поменяться.")
        return
    b = InlineKeyboardBuilder()
    for user, theirs in targets:
        label = f"{user.name} — {theirs.duty.title}" if theirs else f"{user.name} — без обязанности"
        b.button(text=label, callback_data=f"swapto:{assignment.id}:{user.id}")
    b.adjust(1)
    await message.answer(f"{text}\n\nПредложить обмен — выбери участника:", reply_markup=b.as_markup())


@router.message(F.chat.type == "private", F.text == texts.BTN_SWAP)
async def swap_menu(message: Message, session: AsyncSession, state: FSMContext):
    await state.clear()
    user = await svc.get_user_by_tg(session, message.chat.id)
    if user is None:
        await message.answer("Сначала нажми /start")
        return
    for game in await svc.upcoming_games(session, config.now()):
        if game.status == GameStatus.DISTRIBUTED and await svc.user_assignments(session, game.id, user.id):
            await open_swap(message, session, user, game.id)
            return
    await message.answer("У тебя сейчас нет обязанностей на ближайшие игры.")


@router.callback_query(F.data.startswith("swap:"))
async def swap_from_card(cb: CallbackQuery, session: AsyncSession):
    await cb.answer()
    user = await svc.get_user_by_tg(session, cb.from_user.id)
    if user is not None:
        await open_swap(cb.message, session, user, int(cb.data.split(":")[1]))


@router.callback_query(F.data.startswith("swapmine:"))
async def swap_pick_mine(cb: CallbackQuery, session: AsyncSession):
    await cb.answer()
    a = await session.get(Assignment, int(cb.data.split(":")[1]))
    if a is None or a.status != AssignmentStatus.ACTIVE or a.user.telegram_id != cb.from_user.id:
        await cb.message.edit_text("Назначение уже неактуально.")
        return
    await show_targets(cb.message, session, a)


@router.callback_query(F.data.startswith("swapto:"))
async def swap_offer(cb: CallbackQuery, session: AsyncSession, bot: Bot):
    _, assignment_id, target_id = cb.data.split(":")
    me = await svc.get_user_by_tg(session, cb.from_user.id)
    try:
        target = await operations.offer_swap(bot, session, me, int(assignment_id), int(target_id))
    except operations.OpError as e:
        await cb.answer()
        await cb.message.edit_text(texts.h(str(e)))
        return
    await cb.answer()
    await cb.message.edit_text(f"Предложение отправлено: {texts.h(target.name)}. Ждём ответа ⏳")


async def _load_request(cb: CallbackQuery, session: AsyncSession) -> SwapRequest | None:
    req = await session.get(SwapRequest, int(cb.data.split(":")[1]))
    target = await session.get(User, req.to_user_id) if req else None
    if req is None or target is None or target.telegram_id != cb.from_user.id:
        await cb.answer("Это предложение не для тебя.", show_alert=True)
        return None
    return req


@router.callback_query(F.data.startswith("swapok:"))
async def swap_accept(cb: CallbackQuery, session: AsyncSession, bot: Bot):
    req = await _load_request(cb, session)
    if req is None:
        return
    await cb.answer()
    error = await svc.accept_swap(session, req)
    if error:
        await cb.message.edit_text(error)
        return
    game = await svc.get_game(session, req.game_id)
    initiator = await session.get(User, req.from_user_id)
    target = await session.get(User, req.to_user_id)
    mine = await svc.user_assignments(session, game.id, target.id)
    theirs = await svc.user_assignments(session, game.id, initiator.id)
    await notifier.refresh_game(bot, session, game)

    def duty_list(items: list[Assignment]) -> str:
        return ", ".join(a.duty.title for a in items) or "нет обязанностей"

    await cb.message.edit_text(f"✅ Обмен выполнен.\nТвоя обязанность: {duty_list(mine)}")
    await notifier.send_dm(
        bot, initiator,
        f"✅ {texts.h(target.name)} согласился на обмен ({texts.game_header(game)}).\n"
        f"Твоя обязанность теперь: {duty_list(theirs)}",
    )
    if req.to_assignment_id is None:  # отдал свою обязанность, ничего не взяв взамен
        from bot import discipline

        given = await session.get(Assignment, req.from_assignment_id)
        note = await discipline.record_transfer(
            bot, session, game, initiator, given.duty if given else None, target, "swap"
        )
        if note:
            await notifier.send_dm(bot, initiator, note)


@router.callback_query(F.data.startswith("swapno:"))
async def swap_decline(cb: CallbackQuery, session: AsyncSession, bot: Bot):
    req = await _load_request(cb, session)
    if req is None:
        return
    await cb.answer()
    if req.status != SwapStatus.PENDING:
        await cb.message.edit_text("Это предложение уже неактуально.")
        return
    req.status = SwapStatus.DECLINED
    initiator = await session.get(User, req.from_user_id)
    target = await session.get(User, req.to_user_id)
    await cb.message.edit_text("Ты отказался от обмена.")
    await notifier.send_dm(
        bot, initiator,
        f"❌ {texts.h(target.name)} отказался от обмена. Можешь предложить другому игроку: «🔄 Поменяться».",
    )
