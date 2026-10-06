"""Сценарии, общие для хендлеров и планировщика."""

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy.ext.asyncio import AsyncSession

from bot import keyboards, notifier, texts, whatsapp
from bot.config import config
from bot.deadlines import penalties_enabled, rsvp_deadline
from bot.models import DutyPhase, Game, GameStatus, Rsvp, User
from bot.services import games as svc


async def distribute_and_announce(bot: Bot, session: AsyncSession, game: Game) -> str:
    """Распределить обязанности, обновить сообщения в чате и написать назначенным.

    Возвращает отчёт для администратора.
    """
    result = await svc.distribute_game(session, game)
    await notifier.refresh_game(bot, session, game)
    await notifier.announce_new_assignments(bot, game, result.assignments)

    by_status = await svc.participants_by_status(session, game.id)
    yes = len(by_status[Rsvp.YES])
    await notify_staff(bot, session, game, by_status)
    lines = [f"🎯 Обязанности распределены: {texts.game_header(game)}", f"Участников: {yes}", ""]
    lines += texts.duties_block(result.assignments, result.unassigned)
    pending = await svc.pending_after_duties(session, game)
    if pending:
        lines += ["", texts.after_line(pending, after_at(game))]
    if result.unassigned:
        warning = texts.unassigned_warning(result.unassigned)
        lines += ["", warning]
        if game.chat_id and any(d.requires_car for d in result.unassigned):
            await bot.send_message(game.chat_id, texts.no_car_warning())
    if not game.chat_id:
        link = await whatsapp.game_link(bot, game)
        await whatsapp.send_draft(
            bot, whatsapp.duties(game, yes, result.assignments, result.unassigned, link, pending, after_at(game)),
            note="🎯 Обязанности для группы WhatsApp 👇",
        )
    return "\n".join(lines)


async def notify_staff(bot: Bot, session: AsyncSession, game: Game, by_status: dict | None = None) -> None:
    """Сводка для штаба (тренер, директор): кто идёт, кто нет, кто молчит."""
    staff = await svc.staff(session)
    if not staff:
        return
    by_status = by_status or await svc.participants_by_status(session, game.id)
    text = texts.staff_summary(game, by_status, await svc.non_responders(session, game))
    for user in staff:
        await notifier.send_dm(bot, user, text, keyboards.with_app_button(None, game.id))


def after_at(game: Game):
    from datetime import timedelta

    return game.starts_at + timedelta(minutes=config.after_duties_minutes)


async def distribute_after_and_announce(bot: Bot, session: AsyncSession, game: Game) -> str | None:
    """После тренировки: мячи, манишки — среди тех, кто был («Буду»)."""
    if not await svc.active_duties(session, DutyPhase.AFTER):
        game.after_duties_done = True
        return None
    result = await svc.distribute_game(session, game, phase=DutyPhase.AFTER)
    await notifier.refresh_game(bot, session, game)
    for a in result.assignments:
        await notifier.send_dm(
            bot, a.user,
            f"🏁 После тренировки ({texts.game_header(game)}) — на тебе:\n{a.duty.emoji} {texts.h(a.duty.action)}",
            keyboards.with_app_button(None, game.id),
        )
    lines = [f"🏁 <b>После тренировки — {texts.game_header(game)}</b>", ""]
    lines += texts.duties_block(result.assignments, result.unassigned, with_mentions=bool(game.chat_id))
    if game.chat_id:
        await bot.send_message(game.chat_id, "\n".join(lines))
    else:
        await whatsapp.send_draft(
            bot, whatsapp.after_duties(game, result.assignments, result.unassigned),
            note="🏁 Кто что забирает после тренировки — для группы WhatsApp 👇",
        )
    return "\n".join(lines)


async def send_poll_invites(bot: Bot, session: AsyncSession, game: Game, skip: User | None = None) -> str:
    """Разослать игрокам в личку: открыт сбор, кнопки ответа прямо в сообщении."""
    deadline = rsvp_deadline(game)
    text = texts.poll_invite(game, deadline, config.now(), config.penalty_points)
    sent, failed, manual = 0, [], 0
    for user in await svc.roster(session):
        if skip is not None and user.id == skip.id:
            continue
        if user.is_manual:  # без Telegram: дойдёт, только если включил уведомления на сайте
            if await notifier.send_dm(bot, user, text):
                sent += 1
            else:
                manual += 1
            continue
        markup = keyboards.with_web_button(keyboards.with_app_button(keyboards.rsvp(game), game.id), user)
        if await notifier.send_dm(bot, user, text, markup):
            sent += 1
        else:
            failed.append(user.name)
    for user in await svc.staff(session):
        await notifier.send_dm(
            bot, user, texts.staff_new_game(game, deadline, config.now()), keyboards.with_app_button(None, game.id)
        )
    report = f"📣 Опрос отправлен в личку: {sent} {texts.people_word(sent)}."
    if manual:
        report += f"\n✍️ Без Telegram (добавлены вручную): {manual} — отметьте их сами или отправьте им приглашение."
    if failed:
        report += "\nНе доставлено (не писали боту или заблокировали): " + ", ".join(texts.h(n) for n in failed)
    return report


async def send_rsvp_nudge(bot: Bot, session: AsyncSession, game: Game) -> None:
    """Напомнить тем, кто ещё не ответил: лично и списком в общем чате."""
    users = await svc.non_responders(session, game)
    game.rsvp_nudge_sent = True
    now, deadline = config.now(), rsvp_deadline(game)
    yes = await svc.yes_count(session, game.id)
    # «Не знаю» — мягкое напоминание определиться (без минуса).
    for user in (await svc.participants_by_status(session, game.id))[Rsvp.MAYBE]:
        await notifier.send_dm(
            bot, user, texts.maybe_nudge(game, deadline, now, yes),
            keyboards.with_app_button(keyboards.rsvp(game), game.id),
        )
    if not users:
        return
    for user in users:
        await notifier.send_dm(
            bot, user, texts.rsvp_nudge(game, deadline, now, config.penalty_points, yes),
            keyboards.with_app_button(keyboards.rsvp(game), game.id),
        )
    if game.chat_id:
        await bot.send_message(
            game.chat_id, texts.group_nudge(game, users, deadline, now, yes),
            reply_to_message_id=game.announce_message_id, allow_sending_without_reply=True,
        )
    else:
        link = await whatsapp.game_link(bot, game)
        await whatsapp.send_draft(
            bot, whatsapp.nudge(game, users, deadline, now, link, yes),
            note="⏰ Не все отметились — напомните в группе WhatsApp 👇",
        )


async def apply_penalties_and_announce(bot: Bot, session: AsyncSession, game: Game) -> str:
    """Закрытие сбора: минусы молчавшим. Возвращает строку для отчёта админам."""
    if not penalties_enabled(game):  # игру создали впритык — ответить было некогда
        game.penalties_applied = True
        return ""
    return await announce_penalties(bot, session, game, await svc.apply_no_response_penalties(session, game))


async def penalize_late_silent(bot: Bot, session: AsyncSession, game: Game) -> str:
    """Админ вручную: минус молчащим, которых не было в расчёте при закрытии сбора."""
    return await announce_penalties(bot, session, game, await svc.penalize_silent(session, game))


async def announce_penalties(bot: Bot, session: AsyncSession, game: Game, results) -> str:
    if not results:
        return ""
    points = results[0].points
    for r in results:
        await notifier.send_dm(bot, r.user, texts.penalty_dm(game, r.points, r.total, config.penalty_limit))
    if config.penalty_announce and game.chat_id:
        await bot.send_message(game.chat_id, texts.penalty_group(game, [r.user for r in results], points))
    elif config.penalty_announce:
        await whatsapp.send_draft(
            bot, whatsapp.penalties(game, [r.user for r in results], points),
            note="🙈 Кто получил минус — для группы WhatsApp 👇",
        )
    over = [(r.user, r.total) for r in results if config.penalty_limit and r.total >= config.penalty_limit]
    if over:
        await notifier.notify_admins(bot, texts.penalty_limit_admin(over, config.penalty_limit))
    return f"🙈 Не ответили на опрос — по −{points}: " + ", ".join(texts.h(r.user.name) for r in results)


async def redeem_and_announce(bot: Bot, session: AsyncSession, game: Game) -> None:
    """Игра прошла — выполненные обязанности списывают минусы."""
    redeemed = await svc.redeem_penalties(session, game)
    if not redeemed:
        return
    left = await svc.open_penalty_points(session, [u.id for u, _ in redeemed])
    for user, count in redeemed:
        await notifier.send_dm(bot, user, texts.penalty_redeemed(game, count, left.get(user.id, 0)))


async def game_card(
    session: AsyncSession, game: Game, user: User | None, is_admin: bool
) -> tuple[str, InlineKeyboardMarkup | None]:
    """Карточка игры в личке: статус игрока, его обязанность, кнопки."""
    by_status = await svc.participants_by_status(session, game.id)
    lines = [f"<b>{texts.game_header(game)}</b>"]
    loc = texts.location_line(game)
    if loc:
        lines.append(loc)
    if user is not None and user.is_staff:
        text = texts.staff_summary(game, by_status, await svc.non_responders(session, game))
        return text, keyboards.with_app_button(None, game.id)
    lines.append(f"Подтвердили: {len(by_status[Rsvp.YES])}")

    my_duties = []
    if user is not None:
        status = await svc.get_rsvp(session, game.id, user.id)
        lines += ["", "Твой статус: " + (texts.RSVP_LABELS[status] if status else "не отмечен")]
        if game.status == GameStatus.DISTRIBUTED:
            my_duties = await svc.user_assignments(session, game.id, user.id)
            if my_duties:
                lines.append("Твоя обязанность: " + ", ".join(a.duty.title for a in my_duties))

    if game.status == GameStatus.DISTRIBUTED:
        assignments = await svc.active_assignments(session, game.id)
        unassigned = await svc.unassigned_duties(session, game)
        lines += ["", "<b>Обязанности</b>"] + texts.duties_block(assignments, unassigned)
    elif game.status == GameStatus.OPEN:
        lines += ["", "<i>Обязанности ещё не распределены.</i>"]

    b = InlineKeyboardBuilder()
    rsvp = keyboards.rsvp(game)
    if rsvp:
        b.row(*rsvp.inline_keyboard[0])
    if my_duties:
        b.button(text=texts.BTN_SWAP, callback_data=f"swap:{game.id}")
        b.adjust(3, 1)
    if is_admin:
        b.attach(InlineKeyboardBuilder.from_markup(keyboards.game_admin(game)))
        b.row(InlineKeyboardButton(text="📤 Текст для WhatsApp", callback_data=f"wa:{game.id}"))
    app = keyboards.app_button(game.id)
    if app is not None:
        b.row(app)
    markup = b.as_markup()
    return "\n".join(lines), (markup if markup.inline_keyboard else None)


async def whatsapp_snapshot(bot: Bot, session: AsyncSession, game: Game) -> str:
    """Текущее состояние игры для WhatsApp: кто идёт или, после распределения, обязанности."""
    link = await whatsapp.game_link(bot, game)
    by_status = await svc.participants_by_status(session, game.id)
    if game.status == GameStatus.DISTRIBUTED:
        return whatsapp.clean_duties(
            game, len(by_status[Rsvp.YES]),
            await svc.active_assignments(session, game.id), await svc.unassigned_duties(session, game), link,
            await svc.pending_after_duties(session, game), after_at(game),
        )
    return whatsapp.clean(whatsapp.status(game, by_status, link))
