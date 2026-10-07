import re
from datetime import date, datetime, time, timedelta, timezone

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot import actions, audit, keyboards, notifier, operations, texts, whatsapp
from bot.config import config
from bot.middlewares import IsAdmin
from bot.models import Duty, Game, GameStatus, Rsvp, User
from bot.players import player_card, players_list
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


def parse_time(raw: str) -> int | None:
    """Время в минутах от начала дня; «24:00» — полночь в конце дня (1440)."""
    m = re.fullmatch(r"\s*(\d{1,2})[:.\s]?(\d{2})\s*", raw)
    if not m:
        return None
    hours, minutes = int(m[1]), int(m[2])
    if minutes > 59 or hours > 24 or (hours == 24 and minutes):
        return None
    return hours * 60 + minutes


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


async def _ask_kind(message: Message, state: FSMContext, minutes: int) -> None:
    data = await state.get_data()
    starts_at = datetime.combine(date.fromisoformat(data["date"]), time()) + timedelta(minutes=minutes)
    if starts_at <= config.now():
        await message.answer("Это время уже прошло. Укажите время в будущем.")
        return
    await state.update_data(starts_at=starts_at.isoformat())
    await state.set_state(NewGame.kind)
    await message.answer("Что это?", reply_markup=keyboards.kind_choice())


@router.callback_query(NewGame.time, F.data.startswith("newtime:"))
async def new_game_time_cb(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await _ask_kind(cb.message, state, int(cb.data.split(":", 1)[1]))


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
    creator = await svc.get_user_by_tg(session, cb.from_user.id)
    try:
        game, poll_report, hint, announce = await operations.create_game(
            bot, session, creator, data["kind"], datetime.fromisoformat(data["starts_at"]), data.get("location")
        )
    except operations.OpError as e:
        await cb.answer(str(e), show_alert=True)
        return
    where = "Опубликовано в чат команды" if game.chat_id else "Игра создана"
    await cb.message.edit_text(f"✅ {where}: {texts.game_header(game)}")
    await cb.answer()
    text, markup = await actions.game_card(session, game, creator, True)
    await cb.message.answer(f"{text}\n\n{poll_report}\n\n<i>{hint}</i>", reply_markup=markup)
    await whatsapp.send_draft(
        bot, announce, chat_ids=[cb.from_user.id],
        note="📤 Анонс для группы WhatsApp — нажмите кнопку под ним и выберите группу 👇",
    )


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
        await operations.cancel_game(bot, session, game, message.chat.id)
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
    if game is None or duty is None:
        await cb.message.edit_text("Игра неактуальна.")
        return
    try:
        await operations.assign_duty(bot, session, game, duty, user)
    except operations.OpError as e:
        await cb.message.edit_text(texts.h(str(e)))
        return
    await cb.message.edit_text(f"✅ {duty.title} — {texts.h(user.name) if user else 'не назначено'}")


# ----------------------------------------------------------------- обязанности


@router.message(Command("duties"))
async def list_duties(message: Message, session: AsyncSession):
    duties = (await session.scalars(select(Duty).order_by(Duty.sort_order, Duty.id))).all()
    lines = ["<b>Обязанности</b>", ""]
    for d in duties:
        flags = (
            (" · после игры" if d.phase == "after" else " · до игры")
            + (" 🚗 нужна машина" if d.requires_car else "")
            + ("" if d.is_active else " (выключена)")
        )
        lines.append(f"{d.id}. {d.title}{flags}")
    lines += ["", "/add_duty 🩹 Аптечка — добавить", "/add_duty 🎈 Насос машина — нужна машина",
              "/toggle_duty &lt;id&gt; — включить/выключить",
              "/duty_phase &lt;id&gt; — до игры / после игры",
              "Удобнее — в приложении: Профиль → ⚙️ Обязанности"]
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


@router.message(Command("duty_phase"))
async def duty_phase(message: Message, command: CommandObject, session: AsyncSession):
    if not command.args or not command.args.strip().isdigit():
        await message.answer("Формат: /duty_phase &lt;id&gt; (id — из /duties)")
        return
    duty = await session.get(Duty, int(command.args))
    if duty is None:
        await message.answer("Нет такой обязанности.")
        return
    duty.phase = "before" if duty.phase == "after" else "after"
    await message.answer(f"{duty.title}: {'после игры' if duty.phase == 'after' else 'до игры'}.")


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
    await audit.record(session, f"⚙️ Обязанность {duty.title}: {'включена' if duty.is_active else 'выключена'}")
    await message.answer(f"{duty.title}: {'включена' if duty.is_active else 'выключена'}.")


# ----------------------------------------------------------------- состав и минусы


class AdminStates(StatesGroup):
    rename = State()


@router.message(Command("players"))
@router.message(F.text == texts.BTN_PLAYERS)
async def players(message: Message, session: AsyncSession, state: FSMContext):
    await state.clear()
    text, markup = await players_list(session)
    await message.answer(text, reply_markup=markup)


STAFF_TITLES = {"coach": "Тренер", "director": "Директор"}


@router.callback_query(F.data.startswith("pl:"))
async def player_action(cb: CallbackQuery, session: AsyncSession, bot: Bot, state: FSMContext):
    _, action, raw_id = cb.data.split(":")
    if action == "list":
        text, markup = await players_list(session)
        await cb.message.edit_text(text, reply_markup=markup)
        await cb.answer()
        return
    user = await session.get(User, int(raw_id))
    if user is None:
        await cb.answer("Игрок не найден.", show_alert=True)
        return
    if action == "name":
        await state.set_state(AdminStates.rename)
        await state.update_data(rename_user_id=user.id)
        await cb.message.answer(f"Новое имя для «{texts.h(user.name)}»:")
        await cb.answer()
        return
    note = None
    if action != "show":
        try:
            title = STAFF_TITLES.get(action)
            note = await operations.player_action(
                bot, session, user, "staff" if title else action, title, actor_tg=cb.from_user.id
            )
        except operations.OpError as e:
            await cb.answer(str(e), show_alert=True)
            text, markup = await player_card(session, user, cb.from_user.id)  # показать актуальное состояние
            try:
                await cb.message.edit_text(text, reply_markup=markup)
            except Exception:  # noqa: BLE001 — сообщение не изменилось
                pass
            return
    text, markup = await player_card(session, user, cb.from_user.id)
    await cb.message.edit_text(text, reply_markup=markup)
    await cb.answer(note)


@router.message(AdminStates.rename, F.text)
async def player_rename(message: Message, session: AsyncSession, state: FSMContext):
    name = message.text.strip()
    if name.startswith("/") or not (1 <= len(name) <= 64):
        await message.answer("Напишите имя текстом (до 64 символов).")
        return
    user = await session.get(User, (await state.get_data()).get("rename_user_id", 0))
    await state.clear()
    if user is None:
        return
    user.name = name
    text, markup = await player_card(session, user, message.from_user.id)
    await message.answer(text + "\n\n✅ Имя изменено.", reply_markup=markup)


async def penalties_view(session: AsyncSession):
    points = await svc.open_penalty_points(session)
    if not points:
        return "Ни у кого нет минусов 👍", None
    users = {u.id: u for u in await svc.all_users(session)}
    rows = sorted(points.items(), key=lambda kv: (-kv[1], users[kv[0]].name))
    lines = ["<b>⚠️ Минусы за неответы на опросы</b>", ""]
    lines += [f"{texts.h(users[uid].name)} — {n} {texts.minus_word(n)}" for uid, n in rows]
    lines += ["", "Нажмите на игрока, чтобы снять минус (например, была уважительная причина)."]
    b = InlineKeyboardBuilder()
    for uid, n in rows:
        b.button(text=f"{users[uid].name} ({n})", callback_data=f"pens:{uid}")
    b.adjust(2)
    return "\n".join(lines), b.as_markup()


@router.message(Command("penalties"))
async def penalties(message: Message, session: AsyncSession):
    text, markup = await penalties_view(session)
    await message.answer(text, reply_markup=markup)


async def _user_penalties_view(session: AsyncSession, user: User):
    items = await svc.user_penalties(session, user.id)
    lines = [f"<b>{texts.h(user.name)}</b> — минусы", ""]
    b = InlineKeyboardBuilder()
    for p in items:
        when = texts.game_header(p.game) if p.game else texts.fmt_date(p.created_at)
        lines.append(f"• −{p.points}: {texts.penalty_reason(p.reason)} — {when}")
        b.button(text=f"❌ Снять: {texts.fmt_date(p.game.starts_at) if p.game else p.id}", callback_data=f"pendel:{p.id}")
    if not items:
        lines.append("Минусов нет.")
    b.button(text="← Все", callback_data="pens:all")
    b.adjust(1)
    return "\n".join(lines), b.as_markup()


@router.callback_query(F.data.startswith("pens:"))
async def penalties_user(cb: CallbackQuery, session: AsyncSession):
    await cb.answer()
    arg = cb.data.split(":")[1]
    if arg == "all":
        text, markup = await penalties_view(session)
    else:
        user = await session.get(User, int(arg))
        if user is None:
            return
        text, markup = await _user_penalties_view(session, user)
    await cb.message.edit_text(text, reply_markup=markup)


@router.callback_query(F.data.startswith("pendel:"))
async def penalty_cancel(cb: CallbackQuery, session: AsyncSession, bot: Bot):
    penalty = await svc.cancel_penalty(session, int(cb.data.split(":")[1]))
    if penalty is not None:
        await audit.record(session, f"♻️ Снял минус: {(await session.get(User, penalty.user_id)).name}", penalty.game)
    if penalty is None:
        await cb.answer("Минус уже снят или отработан.", show_alert=True)
        return
    user = await session.get(User, penalty.user_id)
    left = (await svc.open_penalty_points(session, [user.id])).get(user.id, 0)
    await notifier.send_dm(bot, user, f"✅ Администратор снял с тебя минус. Осталось: {left}.")
    text, markup = await _user_penalties_view(session, user)
    await cb.message.edit_text(text, reply_markup=markup)
    await cb.answer("Минус снят")


@router.callback_query(F.data.startswith("wa:"))
async def whatsapp_text(cb: CallbackQuery, session: AsyncSession, bot: Bot):
    await cb.answer()
    game = await svc.get_game(session, int(cb.data.split(":")[1]))
    if game is None:
        return
    await whatsapp.send_draft(
        bot, await actions.whatsapp_snapshot(bot, session, game), chat_ids=[cb.from_user.id],
        note="📤 Текущее состояние для группы WhatsApp 👇",
    )


@router.callback_query(F.data.startswith("mind:"))
async def min_decision(cb: CallbackQuery, session: AsyncSession, bot: Bot):
    _, choice, game_id = cb.data.split(":")
    game = await svc.get_game(session, int(game_id))
    if game is None:
        await cb.answer("Игра не найдена.", show_alert=True)
        return
    try:
        note = await operations.decide_min(bot, session, game, choice, cb.from_user.id)
    except operations.OpError as e:
        await cb.answer(str(e), show_alert=True)
        return
    await cb.answer()
    await cb.message.edit_reply_markup(reply_markup=None)
    await cb.message.answer(note)


@router.callback_query(F.data.startswith("att:"))
async def attendance_toggle(cb: CallbackQuery, session: AsyncSession, bot: Bot):
    _, game_id, user_id, present = cb.data.split(":")
    game = await svc.get_game(session, int(game_id))
    user = await session.get(User, int(user_id))
    if game is None or user is None:
        await cb.answer("Не найдено.", show_alert=True)
        return
    try:
        note = await operations.mark_attendance(bot, session, game, user, present == "1")
    except operations.OpError as e:
        await cb.answer(str(e), show_alert=True)
        return
    await cb.message.edit_reply_markup(reply_markup=await operations.attendance_markup(session, game))
    await cb.answer(note)


@router.message(Command("invite"))
async def team_invite(message: Message, bot: Bot):
    me = await bot.me()
    await whatsapp.send_draft(
        bot, whatsapp.team_invite(me.username), chat_ids=[message.chat.id],
        note="📤 Инструкция для команды — отправьте в группу WhatsApp 👇",
    )


# ----------------------------------------------------------------- просьба изменить ответ после закрытия сбора


@router.callback_query(F.data.startswith("rq:") | F.data.startswith("rqno:"))
async def rsvp_request_decision(cb: CallbackQuery, session: AsyncSession, bot: Bot):
    kind, game_id, user_id, status = cb.data.split(":")
    game = await svc.get_game(session, int(game_id))
    user = await session.get(User, int(user_id))
    if game is None or user is None or status not in Rsvp.ALL:
        await cb.answer("Игра или игрок не найдены.", show_alert=True)
        return
    label = texts.RSVP_LABELS[status]
    if kind == "rqno":
        await audit.record(session, f"✖️ Отклонил просьбу {user.name} изменить ответ на «{label}»", game)
        await notifier.send_dm(bot, user, f"✖️ Админ не стал менять твой ответ на «{label}» ({texts.game_header(game)}).")
        await cb.message.edit_text(cb.message.html_text + f"\n\n✖️ Отклонено ({texts.h(cb.from_user.first_name)})")
        await cb.answer("Отклонено")
        return
    try:
        result = await operations.change_rsvp(bot, session, game, user, status, by_admin=True)
    except operations.OpError as e:
        await cb.answer(str(e), show_alert=True)
        return
    note = f"✅ Админ изменил твой ответ: {label} ({texts.game_header(game)})."
    if result.reassigned:
        note += "\nТвоя обязанность передана другому игроку."
    await notifier.send_dm(bot, user, note)
    await cb.message.edit_text(cb.message.html_text + f"\n\n✅ Подтверждено ({texts.h(cb.from_user.first_name)})")
    await cb.answer("Готово")


@router.message(Command("log"))
async def audit_log(message: Message, session: AsyncSession):
    """Журнал действий админов — только главному."""
    if not config.is_owner(message.chat.id):
        await message.answer("Журнал видит только главный админ.")
        return
    rows = await svc.audit_entries(session, limit=25)
    if not rows:
        await message.answer("📜 Журнал пуст — админы пока ничего не делали.")
        return
    lines = ["<b>📜 Последние действия админов</b>", ""]
    for r in rows:
        local = r.created_at.replace(tzinfo=timezone.utc).astimezone(config.tz)
        lines.append(f"{local:%d.%m %H:%M} · <b>{texts.h(r.actor_name)}</b>: {texts.h(r.text)}")
    lines += ["", "Полный журнал с фильтром по админу — в приложении: Профиль → «Журнал действий админов»."]
    await message.answer("\n".join(lines))
