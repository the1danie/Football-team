"""Действия пользователей и админов — одна реализация для кнопок бота и для Mini App.

Ошибки, которые нужно показать человеку, — OpError с понятным текстом.
"""

from datetime import datetime

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy.ext.asyncio import AsyncSession

from bot import actions, keyboards, notifier, texts, whatsapp
from bot.config import config
from bot.deadlines import distribute_at, penalties_enabled, rsvp_deadline
from bot.models import Assignment, AssignmentStatus, Duty, Game, GameStatus, Rsvp, SwapStatus, User, UserStatus
from bot.services import games as svc


class OpError(Exception):
    """Ошибка, текст которой можно показать пользователю."""


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


async def change_rsvp(bot: Bot, session: AsyncSession, game: Game, user: User, status: str) -> svc.RsvpResult:
    if status not in Rsvp.ALL:
        raise OpError("Неизвестный ответ.")
    if game.status not in GameStatus.ACTIVE:
        raise OpError("Сбор по этой игре закрыт.")
    if config.now() >= game.starts_at:
        raise OpError("Игра уже началась.")
    result = await svc.set_rsvp(session, game, user, status)
    if result.changed:
        await notifier.refresh_game(bot, session, game)
        await notifier.announce_reassignments(bot, game, result.reassigned)
        await notifier.announce_new_assignments(bot, game, result.filled)
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
        hint.append(f"Если «Буду» будет меньше {game.min_players}, игра отменится автоматически.")

    announce = whatsapp.announce(game, deadline, now, await whatsapp.game_link(bot, game))
    return game, poll_report, " ".join(hint), announce


def _require_active(game: Game) -> None:
    if game.status not in GameStatus.ACTIVE:
        word = "отменена" if game.status == GameStatus.CANCELLED else "завершена"
        raise OpError(f"{texts.game_header(game)}: игра уже {word}.")


async def distribute(bot: Bot, session: AsyncSession, game: Game) -> str:
    _require_active(game)
    return await actions.distribute_and_announce(bot, session, game)


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
    await notifier.refresh_game(bot, session, game)

    why = f": {reason}" if reason else ""
    if game.chat_id:
        await bot.send_message(
            game.chat_id, f"❌ <b>{texts.game_header(game)} отменена</b>{texts.h(why)}.",
            reply_to_message_id=game.announce_message_id,
        )
    else:
        await whatsapp.send_draft(
            bot, f"❌ *{texts.game_header(game)} отменена*{why}.",
            chat_ids=[admin_chat_id] if admin_chat_id else None,
            note="Сообщите команде в WhatsApp 👇",
        )
    for user in notify.values():
        await notifier.send_dm(bot, user, f"❌ {texts.game_header(game)} отменена{texts.h(why)}.")


async def cancel_if_too_few(bot: Bot, session: AsyncSession, game: Game) -> bool:
    """Сбор закрыт, а «Буду» меньше минимума — отменяем. True, если отменили."""
    if not game.min_players or game.status not in GameStatus.ACTIVE:
        return False
    yes = await svc.yes_count(session, game.id)
    if yes >= game.min_players:
        return False
    await cancel_game(bot, session, game, None, f"не набралось людей — {yes} из {game.min_players}")
    return True


async def create_from_schedule(bot: Bot, session: AsyncSession, schedule, starts_at: datetime) -> Game:
    """Игра по расписанию: создать, разослать опрос, админам — отчёт и анонс для WhatsApp."""
    game, poll_report, hint, announce = await create_game(
        bot, session, None, schedule.kind, starts_at, schedule.location, schedule.min_players, schedule.id
    )
    await notifier.notify_admins(
        bot, f"🔁 По расписанию создана: {texts.game_header(game)}\n\n{poll_report}\n\n<i>{hint}</i>"
    )
    await whatsapp.send_draft(bot, announce, note="📤 Анонс для группы WhatsApp 👇")
    return game


async def assign_duty(bot: Bot, session: AsyncSession, game: Game, duty: Duty, user: User | None) -> None:
    if game.status != GameStatus.DISTRIBUTED:
        raise OpError("Сначала распределите обязанности.")
    if user is not None and duty.requires_car and not user.has_car:
        raise OpError(f"Для «{duty.name}» нужна машина.")
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
    elif action == "rename":
        name = (name or "").strip()
        if not (1 <= len(name) <= 64):
            raise OpError("Имя — от 1 до 64 символов.")
        user.name = name
        note = "Имя изменено"
    else:
        raise OpError("Неизвестное действие.")
    await session.flush()
    return note
