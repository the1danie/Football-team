"""Действия пользователей и админов — одна реализация для кнопок бота и для Mini App.

Ошибки, которые нужно показать человеку, — OpError с понятным текстом.
"""

from datetime import datetime, timedelta

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot import actions, audit, keyboards, notifier, texts, whatsapp
from bot.config import config
from bot.deadlines import distribute_at, penalties_enabled, rsvp_deadline
from bot.models import (
    Assignment, AssignmentStatus, Duty, Game, GameStatus, Penalty, PenaltyReason, PenaltyStatus, Rsvp, SwapStatus, User,
    UserStatus,
)
from bot.services import games as svc


class OpError(Exception):
    """Ошибка, текст которой можно показать пользователю."""


class RsvpLocked(OpError):
    """Сбор закрыт — игрок сам ответ не меняет, только через админа."""


# ----------------------------------------------------------------- регистрация и профиль


async def complete_registration(
    bot: Bot, session: AsyncSession, telegram_id: int, name: str, username: str | None, has_car: bool
) -> User:
    """Сохранить профиль. Новичку без подтверждения — заявка админам."""
    user = await svc.get_or_create_user(session, telegram_id, name, username)
    first_time = not user.profile_completed
    user.name = name[:64]
    if not user.car_locked:
        user.has_car = has_car
    user.profile_completed = True
    await session.flush()
    if first_time and not user.is_approved:
        from bot.players import player_card  # избегаем циклического импорта

        text, markup = await player_card(session, user)
        for admin_id in config.all_admin_ids:
            await notifier.send_raw(bot, admin_id, "🆕 Новый игрок просится в команду:\n\n" + text, markup)
    return user


async def add_manual_player(session: AsyncSession, name: str, has_car: bool) -> User:
    name = (name or "").strip()
    if not (1 <= len(name) <= 64) or name.startswith("/"):
        raise OpError("Имя — от 1 до 64 символов.")
    taken = {u.name.lower() for u in await svc.all_users(session) if u.status != UserStatus.BLOCKED}
    if name.lower() in taken:
        raise OpError("Игрок с таким именем уже есть — добавьте фамилию или букву.")
    user = await svc.create_manual_user(session, name, has_car)
    await audit.record(session, f"✍️ Добавил вручную игрока {user.name}")
    return user


async def link_account(bot: Bot, session: AsyncSession, manual: User, telegram_id: int, username: str | None) -> None:
    """Связать игрока, добавленного вручную, с его Telegram (по ссылке-приглашению или админом)."""
    if not manual.is_manual:
        raise OpError("Этот игрок уже привязан к Telegram.")
    await audit.record(session, f"🔗 Связал {manual.name} с Telegram")
    try:
        await svc.link_telegram(session, manual, telegram_id, username)
    except ValueError:
        raise OpError(
            "У этого Telegram уже есть свой профиль с отметками — связать нельзя. Удалите лишнего игрока вручную."
        ) from None
    await notifier.send_dm(
        bot, manual,
        f"✅ Готово, {texts.h(manual.name)}! Ты в команде — теперь можно самому отмечаться на игры.\n"
        "Минусы и отметки, которые были до этого, сохранились.",
        keyboards.main_menu(config.is_admin(telegram_id)),
    )
    for game in await svc.upcoming_games(session, config.now()):
        if game.status in GameStatus.ACTIVE:
            text, markup = await actions.game_card(session, game, manual, False)
            await notifier.send_dm(bot, manual, text, markup)


async def request_rsvp_change(bot: Bot, session: AsyncSession, game: Game, user: User, status: str) -> str:
    """Сбор закрыт: отправить админам просьбу игрока изменить ответ (кнопки «Подтвердить / Отклонить»)."""
    current = await svc.get_rsvp(session, game.id, user.id)
    if current == status:
        return f"Ты уже отметил: {texts.RSVP_LABELS[status]}"
    duties = await svc.user_assignments(session, game.id, user.id)
    lines = [
        f"✋ <b>{texts.h(user.name)}</b> просит изменить ответ: "
        f"{texts.RSVP_LABELS[current] if current else 'не отвечал'} → <b>{texts.RSVP_LABELS[status]}</b>",
        texts.game_header(game) + " (сбор уже закрыт)",
    ]
    if duties:
        lines.append("Обязанности у него: " + ", ".join(a.duty.title for a in duties))
    markup = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Подтвердить", callback_data=f"rq:{game.id}:{user.id}:{status}"),
        InlineKeyboardButton(text="✖️ Отклонить", callback_data=f"rqno:{game.id}:{user.id}:{status}"),
    ]])
    for admin_id in config.all_admin_ids:
        await notifier.send_raw(bot, admin_id, "\n".join(lines), markup)
    return f"🔒 Сбор закрыт — сам изменить ответ уже нельзя. Запрос «{texts.RSVP_LABELS[status]}» отправлен админу."


async def rename_self(session: AsyncSession, user: User, name: str) -> None:
    name = name.strip()
    if not (1 <= len(name) <= 64) or name.startswith("/"):
        raise OpError("Имя — от 1 до 64 символов.")
    user.name = name
    await session.flush()


async def set_own_car(bot: Bot, session: AsyncSession, user: User, has_car: bool) -> None:
    if user.car_locked and not config.is_admin(user.telegram_id):
        raise OpError("Наличие машины отметил администратор — изменить может только он.")
    had_car = user.has_car
    moved = await svc.set_car(session, user, has_car, config.now(), by_admin=False)
    for game, reassigned in moved:
        await notifier.refresh_game(bot, session, game)
        await notifier.announce_reassignments(bot, game, reassigned)
    if had_car != user.has_car and not config.is_admin(user.telegram_id):
        await notifier.notify_admins(
            bot,
            f"🚗 {texts.h(user.name)} изменил в профиле: "
            f"{texts.CAR_YES if user.has_car else texts.CAR_NO} (было: {texts.CAR_YES if had_car else texts.CAR_NO}).\n"
            "Если это неправда — исправьте и закрепите: 🗂 Игроки.",
        )


# ----------------------------------------------------------------- отметки


async def change_rsvp(
    bot: Bot, session: AsyncSession, game: Game, user: User, status: str, by_admin: bool = False,
    count_transfer: bool = True,
) -> svc.RsvpResult:
    """count_transfer=False — админ отметил сам (например, игрок заболел): обязанность уходит без минуса."""
    if status not in Rsvp.ALL:
        raise OpError("Неизвестный ответ.")
    if game.status not in GameStatus.ACTIVE:
        raise OpError("Сбор по этой игре закрыт.")
    if config.now() >= game.starts_at:
        raise OpError("Игра уже началась.")
    if not by_admin and not config.is_admin(user.telegram_id) and config.now() >= rsvp_deadline(game):
        raise RsvpLocked("🔒 Сбор закрыт — изменить ответ может только админ.")
    if user.is_staff:
        raise OpError(f"Ты в штабе команды ({user.staff_title.lower()}) — отмечаться не нужно, список виден в приложении.")
    result = await svc.set_rsvp(session, game, user, status)
    actor = audit.current()
    if by_admin and result.changed and (actor is None or actor.telegram_id != user.telegram_id):
        await audit.record(session, f"🙋 Отметил за {user.name}: {texts.RSVP_LABELS[status]}", game)
    if result.changed:
        await notifier.refresh_game(bot, session, game)
        await notifier.announce_reassignments(bot, game, result.reassigned)
        await notifier.announce_new_assignments(bot, game, result.filled)
        if count_transfer:  # отказался после распределения — обязанность ушла другому
            from bot import discipline

            mine = [r for r in result.reassigned if r.old_user.id == user.id]
            if mine:  # один отказ — одна передача, сколько бы обязанностей ни было
                note = await discipline.record_transfer(bot, session, game, user, mine[0].duty, mine[0].new_user, "dropped")
                if note:
                    await notifier.send_dm(bot, user, note)
    return result


# ----------------------------------------------------------------- игры (админ)


async def create_game(
    bot: Bot,
    session: AsyncSession,
    creator: User | None,
    kind: str,
    starts_at: datetime,
    location: str | None,
    min_players: int | None = None,
    schedule_id: int | None = None,
    location_url: str | None = None,
) -> tuple[Game, str, str, str]:
    """Создать игру, опубликовать в Telegram-группу (если есть), разослать опрос.

    Возвращает (игра, отчёт о рассылке, подсказка о сроках, анонс для WhatsApp).
    """
    if kind not in texts.KIND_TITLES:
        raise OpError("Выберите: игра или тренировка.")
    if starts_at <= config.now():
        raise OpError("Это время уже прошло.")
    if min_players is not None and not 0 <= min_players <= 100:
        raise OpError("Минимум игроков — от 0 до 100.")
    game = await svc.create_game(
        session, kind, starts_at, (location or "").strip()[:255] or None, creator, min_players, schedule_id
    )
    # Ссылка на карту: вставленная или запомненная для этого места.
    game.location_url = texts.normalize_map_url(location_url) or await svc.known_place_url(session, game.location)
    # Сначала сохранить игру, потом рассылать: если запрос дальше оборвётся (сбой, таймаут),
    # у игроков не останется опроса на игру, которой нет в базе.
    await session.commit()
    chat_id = await notifier.group_chat_id(session)
    if chat_id is not None:
        await notifier.publish_game(bot, session, game, chat_id)
    poll_report = await actions.send_poll_invites(bot, session, game, skip=creator)

    now, deadline = config.now(), rsvp_deadline(game)
    hint = [f"Сбор закрывается {texts.until(deadline, now)}."]
    if distribute_at(game) is not None:
        hint.append("Тогда же обязанности распределятся автоматически.")
    else:
        hint.append("Обязанности распределите вручную.")
    if penalties_enabled(game) and config.penalty_points > 0:
        hint.append(f"Кто не ответит к этому времени — получит −{config.penalty_points}.")
    if game.min_players:
        hint.append(
            f"Если к закрытию сбора «Буду» будет меньше {game.min_players}, "
            + ("игра отменится автоматически." if config.min_players_auto_cancel else "бот спросит вас: проводить или отменить.")
        )

    announce = whatsapp.clean(whatsapp.announce(game, deadline, now, await whatsapp.game_link(bot, game)))
    await audit.record(session, "➕ Создал", game)
    return game, poll_report, " ".join(hint), announce


async def update_game(
    bot: Bot,
    session: AsyncSession,
    game: Game,
    kind: str,
    starts_at: datetime,
    location: str | None,
    min_players: int | None,
    location_url: str | None = None,
) -> tuple[list[str], str]:
    """Изменить игру. Возвращает (что поменялось, текст для WhatsApp).

    Отметившимся и назначенным — личное сообщение с изменениями.
    """
    _require_active(game)
    if kind not in texts.KIND_TITLES:
        raise OpError("Выберите: игра или тренировка.")
    if starts_at <= config.now():
        raise OpError("Это время уже прошло.")
    if min_players is not None and not 0 <= min_players <= 100:
        raise OpError("Минимум игроков — от 0 до 100.")
    location = (location or "").strip()[:255] or None

    changes: list[str] = []
    if starts_at != game.starts_at:
        changes.append(
            f"🕗 {texts.fmt_date(game.starts_at, weekday=True)}, {texts.fmt_time(game.starts_at)} → "
            f"{texts.fmt_date(starts_at, weekday=True)}, {texts.fmt_time(starts_at)}"
        )
        game.starts_at = starts_at
        # Напоминания — заново под новое время.
        game.personal_reminder_sent = False
        game.group_reminder_sent = False
        if not game.penalties_applied:
            game.rsvp_nudge_sent = False
    old_location, old_url = game.location, game.location_url
    if location != old_location:
        changes.append(f"📍 {location or 'место не указано'}" + (f" (было: {old_location})" if old_location else ""))
        game.location = location
    # Ссылка на карту: вставленная; иначе — прежняя (если место то же) или запомненная для нового места.
    pasted = texts.normalize_map_url(location_url)
    if pasted:
        new_url = pasted
    elif location == old_location:
        new_url = old_url
    else:
        game.location_url = None  # иначе сама игра «запомнит» старую ссылку для нового места
        new_url = await svc.known_place_url(session, location)
    if (new_url or None) != (old_url or None):
        if location == old_location:
            changes.append("📍 Обновлена ссылка на место (2ГИС)")
        game.location_url = new_url
    if kind != game.kind:
        changes.append(f"{texts.KIND_TITLES[kind]} (было: {texts.KIND_TITLES.get(game.kind, '')})")
        game.kind = kind
    if (min_players or None) != game.min_players:
        changes.append(f"Минимум игроков: {min_players or 'без минимума'}")
        game.min_players = min_players or None
    if not changes:
        raise OpError("Ничего не изменилось.")
    await session.flush()
    await notifier.refresh_game(bot, session, game)

    by_status = await svc.participants_by_status(session, game.id)
    notify = {u.id: u for u in by_status[Rsvp.YES] + by_status[Rsvp.MAYBE]}
    notify.update({a.user.id: a.user for a in await svc.active_assignments(session, game.id)})
    body = "\n".join(texts.h(c) for c in changes)
    for user in notify.values():
        await notifier.send_dm(
            bot, user, f"✏️ <b>Изменения: {texts.game_header(game)}</b>\n\n{body}",
            keyboards.with_app_button(keyboards.rsvp(game), game.id),
        )
    if game.chat_id:
        await bot.send_message(
            game.chat_id, f"✏️ <b>Изменения: {texts.game_header(game)}</b>\n\n{body}",
            reply_to_message_id=game.announce_message_id,
        )
    link = await whatsapp.game_link(bot, game)
    wa = whatsapp.clean("\n".join([f"*Изменения: {texts.game_header(game)}*", "", *changes, "", "Отметиться:", link]))
    if changes:
        await audit.record(session, "✏️ Изменил: " + "; ".join(changes), game)
    return changes, wa


def _require_active(game: Game) -> None:
    if game.status not in GameStatus.ACTIVE:
        word = "отменена" if game.status == GameStatus.CANCELLED else "завершена"
        raise OpError(f"{texts.game_header(game)}: игра уже {word}.")


async def distribute(bot: Bot, session: AsyncSession, game: Game) -> str:
    _require_active(game)
    if game.min_players:
        game.min_decision = "keep"  # админ распределил сам — значит, проводим
    redo = game.status == GameStatus.DISTRIBUTED
    await audit.record(session, "🎯 Распределил обязанности" + (" заново" if redo else ""), game)
    report = await actions.distribute_and_announce(bot, session, game, reshuffle=redo)
    if redo and game.after_duties_done:  # «после тренировки» уже раздали — пересчитать и их
        after = await actions.distribute_after_and_announce(bot, session, game, reshuffle=True)
        if after:
            report += "\n\n" + after
    return report


async def undistribute(bot: Bot, session: AsyncSession, game: Game) -> str:
    """Главный админ: вернуть игру к сбору (распределили раньше времени)."""
    if game.status != GameStatus.DISTRIBUTED:
        raise OpError("Обязанности ещё не распределены.")
    if config.now() >= game.starts_at:
        raise OpError("Игра уже началась — распределение не отменить.")
    dropped = await svc.undistribute(session, game)
    await audit.record(session, "↩️ Отменил распределение", game)
    await notifier.refresh_game(bot, session, game)
    when = texts.until(rsvp_deadline(game), config.now())
    for user in {a.user_id: a.user for a in dropped}.values():
        await notifier.send_dm(
            bot, user, f"↩️ Распределение на {texts.game_header(game)} отменено. Обязанности распределятся {when}."
        )
    return f"↩️ Распределение отменено. Бот распределит обязанности {when}."


async def cancel_game(
    bot: Bot, session: AsyncSession, game: Game, admin_chat_id: int | None, reason: str | None = None
) -> None:
    """Отменить игру. admin_chat_id=None — отмена автоматическая, текст для WhatsApp — всем админам."""
    _require_active(game)
    # Сообщить всем, кто собирался прийти, и тем, у кого были обязанности.
    by_status = await svc.participants_by_status(session, game.id)
    notify = {u.id: u for u in by_status[Rsvp.YES] + by_status[Rsvp.MAYBE]}
    dropped = await svc.cancel_game(session, game)
    notify.update({a.user.id: a.user for a in dropped})
    game.cancel_reason = (reason or "")[:255] or None
    await audit.record(session, "❌ Отменил" + (f" ({reason})" if reason else ""), game)
    await notifier.refresh_game(bot, session, game)

    why = f": {reason}" if reason else ""
    if game.chat_id:
        await bot.send_message(
            game.chat_id, f"❌ <b>{texts.game_header(game)} отменена</b>{texts.h(why)}.",
            reply_to_message_id=game.announce_message_id,
        )
    else:
        await whatsapp.send_draft(
            bot, f"*{texts.game_header(game)} отменена*{why}.",
            chat_ids=[admin_chat_id] if admin_chat_id else None,
            note="Сообщите команде в WhatsApp 👇",
        )
    notify.update({u.id: u for u in await svc.staff(session)})
    for user in notify.values():
        await notifier.send_dm(bot, user, f"❌ {texts.game_header(game)} отменена{texts.h(why)}.")


MIN_RECHECK = timedelta(hours=1)


def _min_decision_markup(game: Game) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ Провести всё равно", callback_data=f"mind:keep:{game.id}")],
            [InlineKeyboardButton(text="⏳ Подождать ещё час", callback_data=f"mind:wait:{game.id}")],
            [InlineKeyboardButton(text="❌ Отменить", callback_data=f"mind:cancel:{game.id}")],
        ]
    )


async def _ask_min_decision(bot: Bot, game: Game, yes: int) -> None:
    word = texts.KIND_WORDS.get(game.kind, "игра")
    text = (
        f"⚠️ <b>{texts.game_header(game)}</b>\n\n"
        f"Сбор закрыт: «Буду» — {yes} из минимум {game.min_players}.\n"
        f"Что делаем? Пока вы не решили, обязанности не распределяются. "
        f"Если кто-то ещё отметится и минимум наберётся — {word} состоится сама."
    )
    markup = keyboards.with_app_button(_min_decision_markup(game), game.id)
    for admin_id in config.all_admin_ids:
        await notifier.send_raw(bot, admin_id, text, markup)


async def check_min_players(bot: Bot, session: AsyncSession, game: Game) -> str:
    """Сбор закрыт — проверить минимум. "ok" — можно распределять, "waiting" — ждём решения
    админа, "cancelled" — отменено (только при MIN_PLAYERS_AUTO_CANCEL=1)."""
    if not game.min_players or game.status not in GameStatus.ACTIVE or game.min_decision == "keep":
        return "ok"
    yes = await svc.yes_count(session, game.id)
    if yes >= game.min_players:
        if game.min_decision == "asked":  # пока ждали — набрались
            game.min_decision = "keep"
            await notifier.notify_admins(
                bot, f"✅ {texts.game_header(game)}: минимум набрался ({yes} из {game.min_players}) — проводим."
            )
        return "ok"
    if config.min_players_auto_cancel:
        await cancel_game(bot, session, game, None, f"не набралось людей — {yes} из {game.min_players}")
        return "cancelled"
    if game.min_decision is None or (game.min_recheck_at and config.now() >= game.min_recheck_at):
        game.min_decision = "asked"
        game.min_recheck_at = None
        await _ask_min_decision(bot, game, yes)
    return "waiting"


async def decide_min(bot: Bot, session: AsyncSession, game: Game, choice: str, admin_chat_id: int) -> str:
    """Решение админа по недобору: keep / wait / cancel. Возвращает итог для показа."""
    _require_active(game)
    yes = await svc.yes_count(session, game.id)
    await audit.record(session, {"keep": "👍 Недобор: проводим", "wait": "⏳ Недобор: ждём час",
                                 "cancel": "Недобор: отменяем"}.get(choice, choice) + f" ({yes} чел.)", game)
    if choice == "keep":
        game.min_decision = "keep"
        game.min_recheck_at = None
        await session.flush()
        if game.status == GameStatus.OPEN:
            await actions.distribute_and_announce(bot, session, game)
            return f"✅ Проводим ({yes} чел.). Обязанности распределены."
        return f"✅ Проводим ({yes} чел.)."
    if choice == "wait":
        game.min_decision = "asked"
        game.min_recheck_at = config.now() + MIN_RECHECK
        await session.flush()
        await _call_for_players(bot, session, game, yes)
        return "⏳ Ждём час: молчащим и сомневающимся напомнили, потом спрошу снова."
    if choice == "cancel":
        await cancel_game(bot, session, game, admin_chat_id, f"не набралось людей — {yes} из {game.min_players}")
        return "❌ Отменена, все отметившиеся получили сообщение."
    raise OpError("Неизвестное решение.")


async def _call_for_players(bot: Bot, session: AsyncSession, game: Game, yes: int) -> None:
    """«Под вопросом»: напомнить молчащим и «Не знаю», админам — текст для WhatsApp."""
    word = texts.KIND_WORDS.get(game.kind, "игра")
    text = (
        f"⚠️ <b>{texts.game_header(game)}</b>\n\n"
        f"Пока {yes} из минимум {game.min_players} — {word} под вопросом. Придёшь? Отметься 👇"
    )
    users = {u.id: u for u in await svc.non_responders(session, game)}
    users.update({u.id: u for u in (await svc.participants_by_status(session, game.id))[Rsvp.MAYBE]})
    for user in users.values():
        await notifier.send_dm(bot, user, text, keyboards.with_app_button(keyboards.rsvp(game), game.id))
    link = await whatsapp.game_link(bot, game)
    await whatsapp.send_draft(
        bot,
        f"*{texts.game_header(game)}*\n\nПока {yes} из минимум {game.min_players} — {word} под вопросом.\n"
        f"Кто придёт — отметьтесь:\n{link}",
        note="⏳ Позовите ещё людей в WhatsApp 👇",
    )


def needs_min_decision(game: Game, yes: int) -> bool:
    return bool(
        game.min_players and yes < game.min_players and game.status == GameStatus.OPEN
        and game.min_decision == "asked"
    )


async def create_from_schedule(bot: Bot, session: AsyncSession, schedule, starts_at: datetime) -> Game:
    """Игра по расписанию: создать, разослать опрос, админам — отчёт и анонс для WhatsApp."""
    game, poll_report, hint, announce = await create_game(
        bot, session, None, schedule.kind, starts_at, schedule.location, schedule.min_players, schedule.id,
        schedule.location_url,
    )
    await notifier.notify_admins(
        bot, f"🔁 По расписанию создана: {texts.game_header(game)}\n\n{poll_report}\n\n<i>{hint}</i>"
    )
    await whatsapp.send_draft(bot, announce, note="📤 Анонс для группы WhatsApp 👇")
    return game


async def assign_duty(bot: Bot, session: AsyncSession, game: Game, duty: Duty, user: User | None) -> None:
    finished = game.status == GameStatus.FINISHED
    if game.status != GameStatus.DISTRIBUTED and not finished:
        raise OpError("Сначала распределите обязанности.")
    if user is not None and duty.requires_car and not user.has_car and not finished:
        raise OpError(f"Для «{duty.name}» нужна машина.")
    old_user, _ = await svc.set_assignment(session, game, duty, user)
    if finished:
        # Исправление задним числом: статистика и минусы — по факту, сообщения не нужны.
        await svc.fix_redemption(session, game, old_user, user)
        if user is not None:
            p = await session.get(svc.GameParticipant, (game.id, user.id))
            if p is None or p.status != Rsvp.YES or p.attended is False:
                await svc.set_attendance(session, game, user, True)  # выполнил обязанность — значит, был
        await audit.record(
            session, f"🔧 Исправил задним числом {duty.title}: {old_user.name if old_user else '—'} → {user.name if user else 'никто'}", game
        )
        return
    await audit.record(
        session, f"🔧 Назначение {duty.title}: {old_user.name if old_user else '—'} → {user.name if user else 'никто'}", game
    )
    await notifier.refresh_game(bot, session, game)
    if old_user is not None and (user is None or old_user.id != user.id):
        await notifier.send_dm(
            bot, old_user, f"Администратор снял с тебя обязанность {duty.title} ({texts.game_header(game)})."
        )
    if user is not None and (old_user is None or old_user.id != user.id):
        await notifier.send_dm(
            bot, user, f"Тебе назначена обязанность на {texts.game_header(game)}:\n{duty.emoji} {texts.h(duty.action)}"
        )


# ----------------------------------------------------------------- не выполнил обязанность


async def mark_duty_failed(
    bot: Bot, session: AsyncSession, game: Game, duty: Duty, done_by: User | None = None
) -> str:
    """Админ: игрок не выполнил обязанность (не принёс воду…). Минус; если кто-то выручил — засчитать ему."""
    from bot import discipline

    if game.status not in (GameStatus.DISTRIBUTED, GameStatus.FINISHED):
        raise OpError("Обязанности по этой игре не распределялись.")
    if config.now() < game.starts_at - timedelta(hours=1):
        raise OpError("Отметить «не выполнил» можно с часа до начала.")
    current = next((a for a in await svc.active_assignments(session, game.id) if a.duty_id == duty.id), None)
    if current is None:
        raise OpError("Эта обязанность ни на кого не назначена.")
    failed = current.user
    if done_by is not None and done_by.id == failed.id:
        raise OpError("Выберите другого — того, кто выполнил вместо него.")
    current.status = AssignmentStatus.FAILED
    finished = game.status == GameStatus.FINISHED
    if finished:
        await svc.fix_redemption(session, game, failed, None)  # не выполнил — минус не отработан
    if done_by is not None:
        session.add(Assignment(game_id=game.id, user=done_by, duty=duty))
        await session.flush()
        if finished:
            await svc.fix_redemption(session, game, None, done_by)
        p = await session.get(svc.GameParticipant, (game.id, done_by.id))
        if p is None or p.status != Rsvp.YES or p.attended is False:
            await svc.set_attendance(session, game, done_by, True)
    await session.flush()
    total = await discipline.penalize(bot, session, failed, game, discipline.NOT_DONE_POINTS, PenaltyReason.NOT_DONE)
    await audit.record(
        session, f"❌ Не выполнил {duty.title}: {failed.name}" + (f" (выручил {done_by.name})" if done_by else ""), game
    )
    await notifier.refresh_game(bot, session, game)
    if failed.status == UserStatus.APPROVED:
        await notifier.send_dm(
            bot, failed,
            f"❌ Ты не выполнил обязанность {duty.title} ({texts.game_header(game)}) — −{discipline.NOT_DONE_POINTS}. "
            f"Всего минусов: {total}." + (f"\n{discipline.level_text(total)}." if discipline.level_text(total) else ""),
        )
    if done_by is not None:
        await notifier.send_dm(bot, done_by, f"🙌 Спасибо, что выручил: {duty.title} засчитана тебе ({texts.game_header(game)}).")
    return f"❌ {failed.name}: не выполнил {duty.name.lower()} — −{discipline.NOT_DONE_POINTS}"


async def undo_duty_failed(bot: Bot, session: AsyncSession, game: Game, duty: Duty) -> str:
    """Ошибочно отметили «не выполнил» — вернуть как было, минус снять."""
    failed = await session.scalar(
        select(Assignment).where(
            Assignment.game_id == game.id, Assignment.duty_id == duty.id, Assignment.status == AssignmentStatus.FAILED
        ).order_by(Assignment.id.desc()).limit(1)
    )
    if failed is None:
        raise OpError("Нечего отменять.")
    finished = game.status == GameStatus.FINISHED
    for a in await svc.active_assignments(session, game.id):
        if a.duty_id == duty.id:  # тот, кто «выручил», — назначение снимаем
            a.status = AssignmentStatus.CANCELLED
            if finished:
                await svc.fix_redemption(session, game, a.user, None)
    failed.status = AssignmentStatus.ACTIVE
    if finished:
        await svc.fix_redemption(session, game, None, failed.user)
    from bot import discipline

    pen = await session.scalar(
        select(Penalty).where(Penalty.user_id == failed.user_id, Penalty.game_id == game.id,
                              Penalty.reason == PenaltyReason.NOT_DONE, Penalty.status == PenaltyStatus.OPEN)
    )
    if pen is not None:
        pen.points -= discipline.NOT_DONE_POINTS
        if pen.points <= 0:
            pen.points, pen.status = discipline.NOT_DONE_POINTS, PenaltyStatus.CANCELLED
    await session.flush()
    await audit.record(session, f"↩️ Отменил «не выполнил» {duty.title}: {failed.user.name}", game)
    await notifier.refresh_game(bot, session, game)
    await notifier.send_dm(bot, failed.user, f"✅ Отметка «не выполнил» ({duty.title}, {texts.game_header(game)}) снята, минус убран.")
    return f"↩️ {failed.user.name}: отметка «не выполнил» снята"


# ----------------------------------------------------------------- обмены


async def offer_swap(bot: Bot, session: AsyncSession, me: User, assignment_id: int, target_id: int) -> User:
    """Предложить обмен (или передачу) обязанности. Возвращает получателя предложения."""
    a = await session.get(Assignment, assignment_id)
    if a is None or a.status != AssignmentStatus.ACTIVE or a.user_id != me.id:
        raise OpError("Назначение уже неактуально.")
    game = await svc.get_game(session, a.game_id)
    targets = {u.id: (u, theirs) for u, theirs in await svc.swap_targets(session, game, a)}
    if target_id not in targets:
        raise OpError("С этим игроком поменяться нельзя.")
    target, theirs = targets[target_id]
    req = await svc.create_swap(session, game, a, target)

    if theirs:
        offer = (
            f"🔄 {texts.h(me.name)} предлагает обмен обязанностями\n{texts.game_header(game)}\n\n"
            f"{texts.h(me.name)} — {a.duty.title}\n"
            f"Ты — {theirs.duty.title}\n\n"
            f"После обмена: ты — {a.duty.title}, {texts.h(me.name)} — {theirs.duty.title}."
        )
    else:
        offer = (
            f"🔄 {texts.h(me.name)} просит взять его обязанность\n{texts.game_header(game)}\n\n"
            f"{a.duty.emoji} {texts.h(a.duty.action)}"
        )
    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Согласиться", callback_data=f"swapok:{req.id}"),
                InlineKeyboardButton(text="❌ Отказаться", callback_data=f"swapno:{req.id}"),
            ]
        ]
    )
    if not theirs:  # передача в одну сторону — предупредить, если будет минус
        from bot import discipline

        warning = await discipline.warn_before_transfer(session, me.id)
        if warning:
            await notifier.send_dm(bot, me, warning + " Если согласится — минус начислится.")
    if not await notifier.send_dm(bot, target, offer, markup):
        req.status = SwapStatus.EXPIRED
        me_bot = await bot.me()
        raise OpError(
            f"Не удалось отправить предложение: {target.name} ещё не писал боту. "
            f"Попроси его открыть @{me_bot.username}, либо выбери другого игрока."
        )
    return target


# ----------------------------------------------------------------- игроки (админ)


async def _announce_moves(bot: Bot, session: AsyncSession, moves) -> None:
    for game, reassigned in moves:
        await notifier.refresh_game(bot, session, game)
        await notifier.announce_reassignments(bot, game, reassigned)


async def player_action(
    bot: Bot, session: AsyncSession, user: User, action: str, name: str | None = None, actor_tg: int | None = None
) -> str:
    """Действие админа над игроком. Возвращает короткий итог для показа.

    actor_tg — кто делает: выдавать/снимать права и удалять админов может только главный админ.
    """
    now = config.now()
    actor_is_owner = actor_tg is None or config.is_owner(actor_tg)
    target_is_admin = config.is_owner(user.telegram_id) or user.is_admin
    if action in ("admin_on", "admin_off") and not actor_is_owner:
        raise OpError("Выдавать и снимать права админа может только главный админ.")
    if action == "block" and target_is_admin and not actor_is_owner:
        raise OpError("Удалить админа может только главный админ.")
    if config.is_owner(user.telegram_id) and action in ("block", "admin_off"):
        raise OpError("Главного админа нельзя удалить или лишить прав (он задан в настройках ADMIN_IDS).")

    old_name = user.name
    # Уведомление о заявке приходит всем админам — второй и третий нажимают уже после первого.
    if action == "approve" and user.status == UserStatus.APPROVED:
        who = await svc.last_audit_actor(session, f"👤 {user.name}: принял в команду")
        raise OpError(f"{user.name} уже в команде" + (f" — принял {who}" if who else "") + ".")
    if action == "block" and user.status == UserStatus.BLOCKED:
        who = await svc.last_audit_actor(session, f"👤 {user.name}: удалил")
        raise OpError(f"{user.name} уже удалён / заявка уже отклонена" + (f" — {who}" if who else "") + ".")
    if action in ("admin_on", "admin_off"):
        await audit.record(session, f"👑 {user.name}: {'выдал' if action == 'admin_on' else 'снял'} права админа")
    if action == "admin_on":
        if not user.is_approved:
            raise OpError("Сначала примите игрока в команду.")
        user.is_admin = True
        await session.flush()
        await svc.refresh_admins(session)
        await notifier.send_dm(
            bot, user,
            "👑 Тебе выдали права администратора команды: создание игр, распределение обязанностей, "
            "заявки игроков. Меню обновлено — кнопки внизу, а в приложении появилась вкладка «Игроки».",
            keyboards.main_menu(True),
        )
        return f"👑 {user.name} теперь админ"
    if action == "admin_off":
        user.is_admin = False
        await session.flush()
        await svc.refresh_admins(session)
        await notifier.send_dm(bot, user, "Права администратора сняты.", keyboards.main_menu(False))
        return f"{user.name} больше не админ"

    if action == "approve":
        was = user.status
        await svc.set_user_status(session, user, UserStatus.APPROVED, now)
        user.is_active = True
        if was != UserStatus.APPROVED:
            await notifier.send_dm(
                bot, user, "✅ Администратор добавил тебя в команду! Теперь можно отмечаться на игры.",
                keyboards.main_menu(False),
            )
            if keyboards.with_web_button(None, user) is not None:
                await notifier.send_dm(
                    bot, user,
                    "📱 Удобнее всего — кнопкой «Открыть» рядом с полем ввода.\n"
                    "🌐 Не пользуешься Telegram каждый день? Открой сайт — по этой личной ссылке "
                    "браузер запомнит вход (не пересылай её):",
                    keyboards.with_web_button(None, user),
                )
            for game in await svc.upcoming_games(session, now):
                if game.status in GameStatus.ACTIVE:
                    text, markup = await actions.game_card(session, game, user, False)
                    await notifier.send_dm(bot, user, text, markup)
        note = f"✅ {user.name} в команде"
    elif action == "block":
        was = user.status
        user.is_admin = False
        await _announce_moves(bot, session, await svc.set_user_status(session, user, UserStatus.BLOCKED, now))
        if was == UserStatus.PENDING:
            await notifier.send_dm(bot, user, "Заявка отклонена администратором.")
        note = f"⛔ {user.name} заблокирован"
    elif action in ("car", "car_on", "car_off"):
        value = (not user.has_car) if action == "car" else action == "car_on"
        await _announce_moves(bot, session, await svc.set_car(session, user, value, now, by_admin=True))
        await notifier.send_dm(
            bot, user, f"Администратор отметил в профиле: {texts.CAR_YES if user.has_car else texts.CAR_NO}."
        )
        note = f"{user.name}: {'есть машина' if user.has_car else 'нет машины'} (закреплено)"
    elif action == "unlock":
        user.car_locked = False
        note = "Игрок снова может сам менять машину"
    elif action == "active":
        user.is_active = not user.is_active
        note = f"{user.name}: {'в составе' if user.is_active else 'временно не в составе'}"
    elif action == "staff":
        title = (name or "").strip()[:32]
        if not title:
            raise OpError("Укажите роль: тренер, директор…")
        if not user.is_approved:
            raise OpError("Сначала примите человека в команду.")
        await _announce_moves(bot, session, await svc.set_staff(session, user, title, now))
        await notifier.send_dm(
            bot, user,
            f"📋 Администратор отметил тебя в штабе команды: <b>{texts.h(title)}</b>.\n"
            "Отмечаться на игры не нужно — опросов и минусов не будет. Кто идёт, кто нет и кто молчит — "
            "видно в приложении, а перед каждой игрой пришлю сводку.",
            keyboards.with_app_button(None),
        )
        note = f"📋 {user.name}: {title}"
    elif action == "staff_off":
        await svc.set_staff(session, user, None, now)
        await notifier.send_dm(bot, user, "⚽ Ты снова в составе игроков — можно отмечаться на игры.")
        note = f"{user.name} снова игрок"
    elif action == "rename":
        name = (name or "").strip()
        if not (1 <= len(name) <= 64):
            raise OpError("Имя — от 1 до 64 символов.")
        user.name = name
        note = "Имя изменено"
    else:
        raise OpError("Неизвестное действие.")
    await audit.record(session, f"👤 {old_name}: " + {
        "approve": "принял в команду", "block": "удалил из команды / отклонил",
        "car": f"машина — {'есть' if user.has_car else 'нет'}", "car_on": "машина — есть", "car_off": "машина — нет",
        "unlock": "разрешил менять машину самому",
        "active": "вернул в состав" if user.is_active else "убрал из состава", "rename": f"переименовал в «{user.name}»",
        "staff": f"штаб: {user.staff_title}", "staff_off": "вернул в игроки",
    }.get(action, action))
    await session.flush()
    return note


# ----------------------------------------------------------------- кто пришёл


async def mark_attendance(bot: Bot, session: AsyncSession, game: Game, user: User, present: bool) -> str:
    if game.status == GameStatus.CANCELLED:
        raise OpError("Игра отменена.")
    if config.now() < game.starts_at - timedelta(hours=1):
        raise OpError("Отмечать, кто пришёл, можно с часа до начала.")
    result = await svc.set_attendance(session, game, user, present)
    await audit.record(session, f"👥 Кто пришёл: {user.name} — {'пришёл' if present else 'НЕ пришёл'}", game)
    await notifier.refresh_game(bot, session, game)
    await notifier.announce_reassignments(bot, game, result.reassigned)
    if result.penalty_total is not None:
        await notifier.send_dm(
            bot, user,
            f"⚠️ Ты отметил «Буду» на {texts.game_header(game)}, но не пришёл — −{config.no_show_points}. "
            f"Всего минусов: {result.penalty_total}.\nЕсли это ошибка — напиши админу.",
        )
        from bot import discipline

        await discipline.check_level(
            bot, session, user, result.penalty_total - config.no_show_points, result.penalty_total
        )
    if result.penalty_removed:
        await notifier.send_dm(bot, user, f"✅ Минус за неявку ({texts.game_header(game)}) снят.")
    return f"{user.name}: {'пришёл' if present else 'не пришёл'}"


async def attendance_markup(session: AsyncSession, game: Game) -> InlineKeyboardMarkup:
    """Кнопки «кто пришёл» для админов в боте: нажатие переключает отметку."""
    marks = await svc.attendance(session, game.id)
    rows = []
    for u in (await svc.participants_by_status(session, game.id))[Rsvp.YES] + [
        u for u in (await svc.participants_by_status(session, game.id))[Rsvp.NO] if marks.get(u.id) is False
    ]:
        came = marks.get(u.id) is not False
        rows.append([InlineKeyboardButton(
            text=f"{'✅' if came else '❌'} {u.name}", callback_data=f"att:{game.id}:{u.id}:{0 if came else 1}"
        )])
    markup = InlineKeyboardMarkup(inline_keyboard=rows)
    return keyboards.with_app_button(markup, game.id) or markup


async def ask_attendance(bot: Bot, session: AsyncSession, game: Game) -> None:
    game.attendance_asked = True
    text = (
        f"👥 <b>Кто пришёл? {texts.game_header(game)}</b>\n\n"
        "Нажмите на того, кто отметил «Буду», но не пришёл (станет ❌). "
        f"Ему — минус, а обязанности «после тренировки» достанутся тем, кто был. "
        "Кого нет в списке, но пришёл, — отметьте в приложении («👥 Кто пришёл»)."
    )
    markup = await attendance_markup(session, game)
    for admin_id in config.all_admin_ids:
        await notifier.send_raw(bot, admin_id, text, markup)
