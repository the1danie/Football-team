import re
from datetime import date, datetime, time

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot import actions, keyboards, notifier, texts
from bot.config import config
from bot.middlewares import IsAdmin
from bot.models import Duty, Game, GameStatus, Rsvp, User
from bot.services import games as svc

router = Router()
router.message.filter(F.chat.type == "private", IsAdmin())
router.callback_query.filter(IsAdmin())


class NewGame(StatesGroup):
    date = State()
    time = State()
    kind = State()
    location = State()
    confirm = State()


# ----------------------------------------------------------------- создание игры


@router.message(F.text == texts.BTN_CREATE)
@router.message(Command("new"))
async def new_game(message: Message, session: AsyncSession, state: FSMContext):
    await state.clear()
    if await notifier.group_chat_id(session) is None:
        await message.answer(
            "Чат команды не привязан.\n\nДобавьте бота в общий чат команды и отправьте там /bindchat."
        )
        return
    await state.set_state(NewGame.date)
    await message.answer(
        "📅 Выберите дату или напишите её в формате <b>ДД.ММ</b> (например, 10.10):",
        reply_markup=keyboards.date_choice(config.now()),
    )


def parse_date(raw: str, today: date) -> date | None:
    m = re.fullmatch(r"\s*(\d{1,2})[./-](\d{1,2})(?:[./-](\d{2,4}))?\s*", raw)
    if not m:
        return None
    day, month, year = int(m[1]), int(m[2]), m[3]
    try:
        if year:
            y = int(year)
            return date(y + 2000 if y < 100 else y, month, day)
        d = date(today.year, month, day)
        return d if d >= today else date(today.year + 1, month, day)
    except ValueError:
        return None


def parse_time(raw: str) -> time | None:
    m = re.fullmatch(r"\s*(\d{1,2})[:.\s]?(\d{2})\s*", raw)
    if not m:
        return None
    try:
        return time(int(m[1]), int(m[2]))
    except ValueError:
        return None


async def _ask_time(message: Message, state: FSMContext, d: date) -> None:
    await state.update_data(date=d.isoformat())
    await state.set_state(NewGame.time)
    await message.answer(
        f"📅 {texts.fmt_date(datetime.combine(d, time()), weekday=True)}\n\n"
        "🕗 Выберите время или напишите его (например, 20:00):",
        reply_markup=keyboards.time_choice(),
    )


@router.callback_query(NewGame.date, F.data.startswith("newdate:"))
async def new_game_date_cb(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await _ask_time(cb.message, state, date.fromisoformat(cb.data.split(":", 1)[1]))


@router.message(NewGame.date, F.text)
async def new_game_date_text(message: Message, state: FSMContext):
    d = parse_date(message.text, config.now().date())
    if d is None:
        await message.answer("Не понял дату. Напишите в формате ДД.ММ, например 10.10.")
        return
    await _ask_time(message, state, d)


async def _ask_kind(message: Message, state: FSMContext, t: time) -> None:
    data = await state.get_data()
    starts_at = datetime.combine(date.fromisoformat(data["date"]), t)
    if starts_at <= config.now():
        await message.answer("Это время уже прошло. Укажите время в будущем.")
        return
    await state.update_data(starts_at=starts_at.isoformat())
    await state.set_state(NewGame.kind)
    await message.answer("Что это?", reply_markup=keyboards.kind_choice())


@router.callback_query(NewGame.time, F.data.startswith("newtime:"))
async def new_game_time_cb(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    raw = cb.data.split(":", 1)[1]
    await _ask_kind(cb.message, state, time(int(raw[:2]), int(raw[2:])))


@router.message(NewGame.time, F.text)
async def new_game_time_text(message: Message, state: FSMContext):
    t = parse_time(message.text)
    if t is None:
        await message.answer("Не понял время. Напишите, например, 20:00.")
        return
    await _ask_kind(message, state, t)


@router.callback_query(NewGame.kind, F.data.startswith("newkind:"))
async def new_game_kind(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await state.update_data(kind=cb.data.split(":", 1)[1])
    await state.set_state(NewGame.location)
    await cb.message.answer("📍 Место (необязательно) — напишите или пропустите:", reply_markup=keyboards.skip())


async def _ask_confirm(message: Message, state: FSMContext, location: str | None) -> None:
    await state.update_data(location=location)
    await state.set_state(NewGame.confirm)
    data = await state.get_data()
    preview = Game(kind=data["kind"], starts_at=datetime.fromisoformat(data["starts_at"]), location=location,
                   status=GameStatus.OPEN)
    text = texts.announce_text(preview, {s: [] for s in Rsvp.ALL})
    await message.answer(f"Проверьте:\n\n{text}", reply_markup=keyboards.publish_confirm())


@router.callback_query(NewGame.location, F.data == "newloc:skip")
async def new_game_location_skip(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await _ask_confirm(cb.message, state, None)


@router.message(NewGame.location, F.text)
async def new_game_location(message: Message, state: FSMContext):
    await _ask_confirm(message, state, message.text.strip()[:255])


@router.callback_query(NewGame.confirm, F.data == "newgame:abort")
async def new_game_abort(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await cb.message.edit_text("Создание игры отменено.")
    await cb.answer()


@router.callback_query(NewGame.confirm, F.data == "newgame:publish")
async def new_game_publish(cb: CallbackQuery, session: AsyncSession, state: FSMContext, bot: Bot):
    data = await state.get_data()
    await state.clear()
    chat_id = await notifier.group_chat_id(session)
    if chat_id is None:
        await cb.answer("Чат команды не привязан (/bindchat).", show_alert=True)
        return
    creator = await svc.get_user_by_tg(session, cb.from_user.id)
    game = await svc.create_game(
        session, data["kind"], datetime.fromisoformat(data["starts_at"]), data.get("location"), creator
    )
    await notifier.publish_game(bot, session, game, chat_id)
    await cb.message.edit_text(f"✅ Опубликовано в чат команды: {texts.game_header(game)}")
    await cb.answer()
    hint = (
        f"Обязанности распределятся автоматически за {config.auto_distribute_hours:g} ч до начала."
        if config.auto_distribute_hours > 0
        else "Когда сбор закончится, нажмите «🎯 Распределить обязанности»."
    )
    text, markup = await actions.game_card(session, game, creator, True)
    await cb.message.answer(f"{text}\n\n<i>{hint}</i>", reply_markup=markup)


# ----------------------------------------------------------------- действия с игрой

MENU_ACTIONS = {
    texts.BTN_PARTICIPANTS: "parts",
    texts.BTN_DISTRIBUTE: "dist",
    texts.BTN_EDIT: "edit",
    texts.BTN_CANCEL: "cancel",
}


@router.message(F.text.in_(MENU_ACTIONS.keys()))
async def menu_action(message: Message, session: AsyncSession, state: FSMContext, bot: Bot):
    await state.clear()
    action = MENU_ACTIONS[message.text]
    games = await svc.upcoming_games(session, config.now())
    if not games:
        await message.answer("Запланированных игр нет. Нажмите «➕ Создать игру».")
        return
    if len(games) > 1:
        await message.answer("Выберите игру:", reply_markup=keyboards.game_picker(games, action))
        return
    await run_action(message, session, bot, action, games[0])


@router.callback_query(F.data.startswith("adm:"))
async def admin_callback(cb: CallbackQuery, session: AsyncSession, bot: Bot):
    _, action, game_id = cb.data.split(":")
    game = await svc.get_game(session, int(game_id))
    await cb.answer()
    if game is None:
        await cb.message.answer("Игра не найдена.")
        return
    await run_action(cb.message, session, bot, action, game)


async def run_action(message: Message, session: AsyncSession, bot: Bot, action: str, game: Game) -> None:
    if action == "parts":
        await message.answer(await participants_text(session, game))
        return

    if game.status not in GameStatus.ACTIVE:
        await message.answer(f"{texts.game_header(game)}: игра уже {'отменена' if game.status == GameStatus.CANCELLED else 'завершена'}.")
        return

    if action == "dist":
        if game.status == GameStatus.DISTRIBUTED:
            await message.answer(
                "Обязанности уже распределены. Пересчитать заново?",
                reply_markup=keyboards.confirm(f"adm:redist:{game.id}", "🔁 Пересчитать"),
            )
            return
        await message.answer(await actions.distribute_and_announce(bot, session, game))
    elif action == "redist":
        await message.answer(await actions.distribute_and_announce(bot, session, game))
    elif action == "edit":
        await show_edit(message, session, game)
    elif action == "cancel":
        await message.answer(
            f"Отменить {texts.game_header(game)}?",
            reply_markup=keyboards.confirm(f"adm:cancelyes:{game.id}", "❌ Да, отменить"),
        )
    elif action == "cancelyes":
        dropped = await svc.cancel_game(session, game)
        await notifier.refresh_game(bot, session, game)
        if game.chat_id:
            await bot.send_message(
                game.chat_id,
                f"❌ <b>{texts.game_header(game)} отменена.</b>",
                reply_to_message_id=game.announce_message_id,
            )
        for user in {a.user.id: a.user for a in dropped}.values():
            await notifier.send_dm(bot, user, f"❌ {texts.game_header(game)} отменена. Обязанности сняты.")
        await message.answer(f"Готово: {texts.game_header(game)} отменена.")


async def participants_text(session: AsyncSession, game: Game) -> str:
    by_status = await svc.participants_by_status(session, game.id)
    lines = [f"<b>👥 Участники — {texts.game_header(game)}</b>", ""]
    for status in (Rsvp.YES, Rsvp.MAYBE, Rsvp.NO):
        users = by_status[status]
        lines.append(f"<b>{texts.RSVP_LABELS[status]} — {len(users)}</b>")
        lines += [f"• {texts.h(u.name)}{' 🚗' if u.has_car else ''}" for u in users] or ["—"]
        lines.append("")
    cars = sum(u.has_car for u in by_status[Rsvp.YES])
    lines.append(f"🚗 С машиной среди «Буду»: {cars}")
    return "\n".join(lines)


# ----------------------------------------------------------------- ручное назначение


async def show_edit(message: Message, session: AsyncSession, game: Game) -> None:
    if game.status != GameStatus.DISTRIBUTED:
        await message.answer("Сначала распределите обязанности: «🎯 Распределить обязанности».")
        return
    current = {a.duty_id: a.user for a in await svc.active_assignments(session, game.id)}
    b = InlineKeyboardBuilder()
    for duty in await svc.active_duties(session):
        who = current[duty.id].name if duty.id in current else "не назначено"
        b.button(text=f"{duty.title} — {who}", callback_data=f"eddu:{game.id}:{duty.id}")
    b.adjust(1)
    await message.answer(f"✏️ {texts.game_header(game)}\nКакую обязанность изменить?", reply_markup=b.as_markup())


@router.callback_query(F.data.startswith("eddu:"))
async def edit_pick_user(cb: CallbackQuery, session: AsyncSession):
    _, game_id, duty_id = cb.data.split(":")
    game = await svc.get_game(session, int(game_id))
    duty = await session.get(Duty, int(duty_id))
    await cb.answer()
    if game is None or duty is None:
        return
    users = (await svc.participants_by_status(session, game.id))[Rsvp.YES]
    b = InlineKeyboardBuilder()
    for u in users:
        if duty.requires_car and not u.has_car:
            continue
        b.button(text=f"{u.name}{' 🚗' if u.has_car else ''}", callback_data=f"edset:{game.id}:{duty.id}:{u.id}")
    b.button(text="🚫 Снять назначение", callback_data=f"edset:{game.id}:{duty.id}:0")
    b.adjust(2)
    note = "\n(показаны только игроки с машиной)" if duty.requires_car else ""
    await cb.message.edit_text(f"{duty.title}: кого назначить?{note}", reply_markup=b.as_markup())


@router.callback_query(F.data.startswith("edset:"))
async def edit_set(cb: CallbackQuery, session: AsyncSession, bot: Bot):
    _, game_id, duty_id, user_id = cb.data.split(":")
    game = await svc.get_game(session, int(game_id))
    duty = await session.get(Duty, int(duty_id))
    user = await session.get(User, int(user_id)) if user_id != "0" else None
    await cb.answer()
    if game is None or duty is None or game.status != GameStatus.DISTRIBUTED:
        await cb.message.edit_text("Игра неактуальна.")
        return
    old_user, _ = await svc.set_assignment(session, game, duty, user)
    await notifier.refresh_game(bot, session, game)
    if old_user is not None and (user is None or old_user.id != user.id):
        await notifier.send_dm(
            bot, old_user, f"Администратор снял с тебя обязанность {duty.title} ({texts.game_header(game)})."
        )
    if user is not None and (old_user is None or old_user.id != user.id):
        await notifier.send_dm(
            bot, user, f"Тебе назначена обязанность на {texts.game_header(game)}:\n{duty.emoji} {texts.h(duty.action)}"
        )
    await cb.message.edit_text(f"✅ {duty.title} — {texts.h(user.name) if user else 'не назначено'}")


# ----------------------------------------------------------------- обязанности


@router.message(Command("duties"))
async def list_duties(message: Message, session: AsyncSession):
    duties = (await session.scalars(select(Duty).order_by(Duty.sort_order, Duty.id))).all()
    lines = ["<b>Обязанности</b>", ""]
    for d in duties:
        flags = (" 🚗 нужна машина" if d.requires_car else "") + ("" if d.is_active else " (выключена)")
        lines.append(f"{d.id}. {d.title}{flags}")
    lines += ["", "/add_duty 🩹 Аптечка — добавить", "/add_duty 🎈 Насос машина — нужна машина",
              "/toggle_duty &lt;id&gt; — включить/выключить"]
    await message.answer("\n".join(lines))


@router.message(Command("add_duty"))
async def add_duty(message: Message, command: CommandObject, session: AsyncSession):
    parts = (command.args or "").split()
    if not parts:
        await message.answer("Формат: /add_duty 🩹 Аптечка [машина]")
        return
    requires_car = parts[-1].lower() in ("машина", "car")
    if requires_car:
        parts = parts[:-1]
    emoji = "📌"
    if parts and not any(ch.isalnum() for ch in parts[0]):
        emoji, parts = parts[0], parts[1:]
    name = " ".join(parts).strip()
    if not name:
        await message.answer("Укажите название обязанности.")
        return
    duty = await svc.add_duty(session, emoji, name[:64], requires_car)
    await message.answer(f"✅ Добавлена обязанность: {duty.title}" + (" (нужна машина)" if requires_car else ""))


@router.message(Command("toggle_duty"))
async def toggle_duty(message: Message, command: CommandObject, session: AsyncSession):
    if not command.args or not command.args.strip().isdigit():
        await message.answer("Формат: /toggle_duty &lt;id&gt; (id — из /duties)")
        return
    duty = await session.get(Duty, int(command.args))
    if duty is None:
        await message.answer("Нет такой обязанности.")
        return
    duty.is_active = not duty.is_active
    await message.answer(f"{duty.title}: {'включена' if duty.is_active else 'выключена'}.")
