"""Фоновые задачи: автораспределение, напоминания, завершение игр.

Каждую минуту проверяем игры в БД — состояние хранится в самих играх,
поэтому после перезапуска бота ничего не теряется.
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from aiogram import Bot
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bot import actions, notifier, texts
from bot.config import config
from bot.models import Game, GameStatus
from bot.services import games as svc

log = logging.getLogger(__name__)
TICK_SECONDS = 60


def distribute_at(game: Game) -> datetime | None:
    """Когда автоматически распределять обязанности.

    Обычно за AUTO_DISTRIBUTE_HOURS до начала. Если игру создали позже этого
    момента — перед личным напоминанием, чтобы игроки успели отметиться.
    """
    if config.auto_distribute_hours <= 0:
        return None
    created = game.created_at.replace(tzinfo=timezone.utc).astimezone(config.tz).replace(tzinfo=None)
    for hours in (config.auto_distribute_hours, config.personal_reminder_hours):
        moment = game.starts_at - timedelta(hours=hours)
        if moment > created:
            return moment
    return None  # создана совсем впритык — распределит администратор


async def tick(bot: Bot, session: AsyncSession) -> None:
    now = config.now()
    games = (await session.scalars(select(Game).where(Game.status.in_(GameStatus.ACTIVE)))).all()
    for game in games:
        if now >= game.starts_at + timedelta(hours=config.finish_after_hours):
            game.status = GameStatus.FINISHED
            await notifier.refresh_game(bot, session, game)
            continue
        if now >= game.starts_at:
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
