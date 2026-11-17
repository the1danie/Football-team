"""Фоновые задачи: автораспределение, напоминания, завершение игр.

Каждую минуту проверяем игры в БД — состояние хранится в самих играх,
поэтому после перезапуска бота ничего не теряется.
"""

import asyncio
import logging
from datetime import timedelta

from aiogram import Bot
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bot import actions, notifier, operations, texts, whatsapp
from bot.config import config
from bot.deadlines import distribute_at, rsvp_deadline, rsvp_reminder_at
from bot.models import Game, GameStatus
from bot.services import games as svc

log = logging.getLogger(__name__)
TICK_SECONDS = 60


async def tick(bot: Bot, session: AsyncSession) -> None:
    await svc.refresh_admins(session)
    now = config.now()
    # Повторяющиеся тренировки: создать очередную, когда открылось окно опроса.
    for schedule, starts_at in await svc.due_schedule_games(session, now):
        try:
            await operations.create_from_schedule(bot, session, schedule, starts_at)
            await session.commit()
        except Exception:  # одна неудача не должна откатывать остальное
            log.exception("Schedule %s: game not created", schedule.id)
            await session.rollback()

    # Разовые игры, созданные заранее: пора выложить — опрос игрокам и анонс админам.
    for game in (await session.scalars(select(Game).where(
            Game.status == GameStatus.PLANNED, Game.publish_at <= now))).all():
        try:
            poll_report, hint, announce = await operations.publish_planned(bot, session, game)
            await notifier.notify_admins(
                bot, f"📣 Выложена: {texts.game_header(game)}\n\n{poll_report}\n\n<i>{hint}</i>"
                     "\n\n📤 Анонс для WhatsApp — следующим сообщением 👇")
            await whatsapp.send_draft(bot, announce)
            await session.commit()
        except operations.OpError as e:  # время игры прошло — сообщить, не выкладывать
            game.publish_at = None
            await notifier.notify_admins(bot, f"⚠️ {texts.game_header(game)} не выложена: {texts.h(str(e))}")
            await session.commit()
        except Exception:
            log.exception("Planned game %s: publish failed", game.id)
            await session.rollback()

    game_ids = (await session.scalars(select(Game.id).where(Game.status.in_(GameStatus.ACTIVE)))).all()
    for game_id in game_ids:
        game = await session.get(Game, game_id)
        if game is None or game.status not in GameStatus.ACTIVE:
            continue
        try:
            await _process_game(bot, session, game, now)
            await session.commit()  # каждая игра — отдельно: сбой в одной не откатывает другие
        except Exception:
            log.exception("Game %s: tick failed", game_id)
            await session.rollback()

    try:  # 1-го числа — отчёты за прошлый месяц админам и штабу
        from bot import monthly

        await monthly.send_if_due(bot, session, now)
        await session.commit()
    except Exception:
        log.exception("Monthly reports failed")
        await session.rollback()


async def _process_game(bot: Bot, session: AsyncSession, game: Game, now) -> None:
    # Через 15 минут после начала — админам список «кто пришёл».
    if (
        game.status == GameStatus.DISTRIBUTED
        and not game.attendance_asked
        and now >= game.starts_at + timedelta(minutes=config.attendance_ask_minutes)
        and now < game.starts_at + timedelta(hours=config.finish_after_hours)
    ):
        await operations.ask_attendance(bot, session, game)

    # После тренировки: мячи, манишки — среди тех, кто был.
    if (
        game.status == GameStatus.DISTRIBUTED
        and not game.after_duties_done
        and now >= game.starts_at + timedelta(minutes=config.after_duties_minutes)
    ):
        await actions.distribute_after_and_announce(bot, session, game)

    if now >= game.starts_at + timedelta(hours=config.finish_after_hours):
        if game.status == GameStatus.DISTRIBUTED:
            await actions.redeem_and_announce(bot, session, game)
        game.status = GameStatus.FINISHED
        await notifier.refresh_game(bot, session, game)
        if not game.result_asked:
            await actions.ask_result(bot, session, game)
        return

    # Закрытие сбора: минусы, «Не буду», распределение — админам одной сводкой и одним текстом для WhatsApp.
    digest = {"admin": [], "wa_duties": None, "wa_penalties": None, "penalized": False}
    await _close_and_distribute(bot, session, game, now, digest)
    await send_digest(bot, game, digest)  # при сбое тик откатится — и сводка не уйдёт


async def _close_and_distribute(bot: Bot, session: AsyncSession, game: Game, now, digest: dict) -> None:
    # Сбор закрыт: минусы тем, кто так и не ответил (до распределения в том же тике).
    if not game.penalties_applied and now >= rsvp_deadline(game):
        report = await actions.apply_penalties_and_announce(bot, session, game, digest)
        if report:
            digest["admin"].append(report)
            digest["penalized"] = True
    # Сразу после минусов: «Не знаю» и молчащие → «Не буду», чтобы вода распределилась только среди «Буду».
    # Пока решается недобор (админ может «подождать час» и позвать сомневающихся) — не трогаем.
    if not game.auto_declined and now >= rsvp_deadline(game) and not (
        game.status == GameStatus.OPEN and game.min_players and game.min_decision != "keep"
        and await svc.yes_count(session, game.id) < game.min_players
    ):
        await actions.auto_decline_and_notify(bot, session, game, digest)
    if now >= game.starts_at:
        return

    # Напоминание молчащим (даже если админ уже распределил вручную — срок сбора тот же).
    nudge_at = rsvp_reminder_at(game)
    if not game.rsvp_nudge_sent and not game.penalties_applied and nudge_at is not None and now >= nudge_at:
        await actions.send_rsvp_nudge(bot, session, game)

    # Сбор закрыт, а минимум не набран — ждём решения админа (или отменяем при автоотмене).
    if game.status == GameStatus.OPEN and now >= rsvp_deadline(game):
        verdict = await operations.check_min_players(bot, session, game)
        if verdict == "cancelled":
            digest["admin"].append(f"❌ Отменена: {texts.h(game.cancel_reason)}.")
            return
        if verdict == "waiting":
            await session.flush()
            return

    auto_at = distribute_at(game)
    if game.status == GameStatus.OPEN and auto_at is not None and now >= auto_at:
        report = await actions.distribute_and_announce(bot, session, game, digest=digest)
        digest["admin"].append("🎯 " + report)

    if game.status != GameStatus.DISTRIBUTED:
        return

    if not game.personal_reminder_sent and now >= game.starts_at - timedelta(hours=config.personal_reminder_hours):
        by_user: dict[int, list] = {}
        for a in await svc.active_assignments(session, game.id):
            by_user.setdefault(a.user_id, []).append(a)
        for items in by_user.values():
            await notifier.send_dm(bot, items[0].user, texts.personal_reminder(game, now, [a.duty for a in items]))
        game.personal_reminder_sent = True

    if not game.group_reminder_sent and now >= game.starts_at - timedelta(hours=config.group_reminder_hours):
        assignments = await svc.active_assignments(session, game.id)
        if game.chat_id and assignments:
            await bot.send_message(game.chat_id, texts.group_reminder(game, now, assignments))
        elif assignments:
            await whatsapp.send_draft(bot, whatsapp.reminder(game, now, assignments))
        game.group_reminder_sent = True

    await session.flush()


async def send_digest(bot: Bot, game: Game, digest: dict) -> None:
    """Сводка закрытия сбора: одно сообщение админам и (если чата нет) один текст для WhatsApp с кнопкой."""
    if digest["admin"]:
        await notifier.notify_admins(
            bot, f"🔒 <b>Сбор закрыт — {texts.game_header(game)}</b>\n\n" + "\n\n".join(digest["admin"]))
    pen = digest.get("wa_penalties")
    pen_line = f"Не ответили на опрос: {', '.join(u.name for u in pen[0])} — по −{pen[1]}." if pen else None
    if digest.get("wa_duties"):
        await whatsapp.send_draft(bot, digest["wa_duties"] + (f"\n\n{pen_line}" if pen_line else ""))
    elif pen:
        await whatsapp.send_draft(bot, whatsapp.penalties(game, *pen))


async def run_scheduler(bot: Bot, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    while True:
        try:
            async with sessionmaker() as session:
                await tick(bot, session)
                await session.commit()
        except Exception:
            log.exception("Scheduler tick failed")
        await asyncio.sleep(TICK_SECONDS)
