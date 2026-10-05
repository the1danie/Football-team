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
        except operations.OpError:
            log.exception("Schedule %s: game not created", schedule.id)
    await session.flush()

    games = (await session.scalars(select(Game).where(Game.status.in_(GameStatus.ACTIVE)))).all()
    for game in games:
        # После тренировки: мячи, манишки, стирка — среди тех, кто был.
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
            continue

        # Сбор закрыт: минусы тем, кто так и не ответил (до распределения в том же тике).
        if not game.penalties_applied and now >= rsvp_deadline(game):
            report = await actions.apply_penalties_and_announce(bot, session, game)
            if report:
                await notifier.notify_admins(bot, report)
        if now >= game.starts_at:
            continue

        # Напоминание молчащим (даже если админ уже распределил вручную — срок сбора тот же).
        nudge_at = rsvp_reminder_at(game)
        if not game.rsvp_nudge_sent and not game.penalties_applied and nudge_at is not None and now >= nudge_at:
            await actions.send_rsvp_nudge(bot, session, game)

        # Сбор закрыт, а минимум не набран — ждём решения админа (или отменяем при автоотмене).
        if game.status == GameStatus.OPEN and now >= rsvp_deadline(game):
            verdict = await operations.check_min_players(bot, session, game)
            if verdict == "cancelled":
                await notifier.notify_admins(bot, f"❌ {texts.game_header(game)} отменена: {texts.h(game.cancel_reason)}.")
                continue
            if verdict == "waiting":
                await session.flush()
                continue

        auto_at = distribute_at(game)
        if game.status == GameStatus.OPEN and auto_at is not None and now >= auto_at:
            report = await actions.distribute_and_announce(bot, session, game)
            await notifier.notify_admins(bot, "🤖 Автоматическое распределение\n\n" + report)

        if game.status != GameStatus.DISTRIBUTED:
            continue

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
                await whatsapp.send_draft(
                    bot, whatsapp.reminder(game, now, assignments),
                    note="⚽ Напоминание перед игрой — для группы WhatsApp 👇",
                )
            game.group_reminder_sent = True

        await session.flush()


async def run_scheduler(bot: Bot, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    while True:
        try:
            async with sessionmaker() as session:
                await tick(bot, session)
                await session.commit()
        except Exception:
            log.exception("Scheduler tick failed")
        await asyncio.sleep(TICK_SECONDS)
