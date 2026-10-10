"""Дисциплина: «не выполнил», «отдал обязанность другому», пороги минусов.

Правила (числа — здесь, в одном месте):
- не выполнил обязанность — NOT_DONE_POINTS;
- отдал обязанность другому: 1-й раз за TRANSFER_WINDOW — бесплатно, 2-й — минус 1,
  3-й — минус 2, 4-й — минус 3 и т. д. (каждый следующий на 1 больше);
- всего минусов 3 → 25 бёрпи на тренировке, 6 → 50 бёрпи, 9 → исключение из команды.
Минусы, как и раньше, отрабатываются выполненными обязанностями (одна — один минус).
"""

from datetime import datetime, timedelta

from aiogram import Bot
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from bot import notifier, texts
from bot.config import config
from bot.models import DutyTransfer, Game, Penalty, PenaltyReason, PenaltyStatus, User, UserStatus
from bot.services import games as svc

NOT_DONE_POINTS = 2
TRANSFER_WINDOW = timedelta(days=30)
EXCLUDE_AT = 9
BURPEES = ((6, 50), (3, 25))  # (минусов от, бёрпи)


def transfer_points(n: int) -> int:
    """Сколько минусов за n-ю передачу обязанности за окно (n начинается с 1)."""
    return max(0, n - 1)


def burpees(total: int) -> int | None:
    for limit, count in BURPEES:
        if total >= limit:
            return count
    return None


def level_text(total: int) -> str | None:
    if total >= EXCLUDE_AT:
        return f"−{total}: исключение из команды"
    b = burpees(total)
    if b:
        return f"−{total}: на тренировке {b} бёрпи. При −{EXCLUDE_AT} — исключение из команды"
    return None


async def add_penalty(session: AsyncSession, user: User, game: Game | None, points: int, reason: str) -> int:
    """Начислить минус (по одной игре и причине — одна запись, баллы суммируются). Возвращает итог."""
    existing = await session.scalar(
        select(Penalty).where(
            Penalty.user_id == user.id, Penalty.game_id == (game.id if game else None), Penalty.reason == reason
        )
    ) if game is not None else None
    if existing is not None:
        if existing.status != PenaltyStatus.OPEN:
            existing.status, existing.points, existing.redeemed_game_id = PenaltyStatus.OPEN, 0, None
        existing.points += points
    else:
        session.add(Penalty(user_id=user.id, game_id=game.id if game else None, points=points, reason=reason))
    await session.flush()
    return (await svc.open_penalty_points(session, [user.id])).get(user.id, 0)


async def check_level(bot: Bot, session: AsyncSession, user: User, before: int, after: int) -> None:
    """Перешёл порог — сообщить игроку и админам; на EXCLUDE_AT — исключить из команды."""
    crossed = [lim for lim in (3, 6, EXCLUDE_AT) if before < lim <= after]
    if not crossed:
        return
    if after >= EXCLUDE_AT and user.status == UserStatus.APPROVED:
        moves = await svc.set_user_status(session, user, UserStatus.BLOCKED, config.now())
        user.is_admin = False
        for game, reassigned in moves:
            await notifier.refresh_game(bot, session, game)
            await notifier.announce_reassignments(bot, game, reassigned)
        await notifier.send_dm(
            bot, user, f"⛔ У тебя −{after}. По правилам команды при −{EXCLUDE_AT} — исключение. Вопросы — к админу."
        )
        await notifier.notify_admins(
            bot, f"⛔ {texts.h(user.name)}: −{after} — исключён из команды по правилам. "
                 "Вернуть можно в «Игроки» → «Удалены» → «♻️ Вернуть в команду» (минусы стоит снять)."
        )
        return
    b = burpees(after)
    await notifier.send_dm(
        bot, user, f"❗ У тебя −{after}: на ближайшей тренировке — {b} бёрпи. При −{EXCLUDE_AT} — исключение из команды.\n"
                   "Минусы снимаются выполненными обязанностями: одна обязанность — один минус."
    )
    await notifier.notify_admins(bot, f"❗ {texts.h(user.name)}: −{after} — на тренировке {b} бёрпи.")


async def penalize(bot: Bot, session: AsyncSession, user: User, game: Game | None, points: int, reason: str) -> int:
    before = (await svc.open_penalty_points(session, [user.id])).get(user.id, 0)
    after = await add_penalty(session, user, game, points, reason)
    await check_level(bot, session, user, before, after)
    return after


async def transfers_in_window(session: AsyncSession, user_id: int, now: datetime | None = None) -> int:
    since = (now or datetime.utcnow()) - TRANSFER_WINDOW
    return int(await session.scalar(
        select(func.count()).select_from(DutyTransfer)
        .where(DutyTransfer.user_id == user_id, DutyTransfer.created_at >= since)
    ) or 0)


async def record_transfer(
    bot: Bot, session: AsyncSession, game: Game, user: User, duty, to_user: User | None, kind: str
) -> str | None:
    """Игрок отдал обязанность. Возвращает пояснение для него (или None, если без последствий)."""
    session.add(DutyTransfer(user_id=user.id, game_id=game.id, duty_id=duty.id if duty else None,
                             to_user_id=to_user.id if to_user else None, kind=kind))
    await session.flush()
    n = await transfers_in_window(session, user.id)
    points = transfer_points(n)
    days = TRANSFER_WINDOW.days
    if not points:
        return f"Ты отдал обязанность другому. Первый раз за {days} дней — без минуса, дальше: −1, −2, −3 и т. д."
    total = await penalize(bot, session, user, game, points, PenaltyReason.GAVE_AWAY)
    await notifier.notify_admins(
        bot, f"🔁 {texts.h(user.name)} отдал обязанность другому — {n}-й раз за {days} дней: −{points} (всего −{total})."
    )
    return f"⚠️ Ты отдаёшь обязанность уже {n}-й раз за {days} дней: −{points}. Всего минусов: {total}."


async def warn_before_transfer(session: AsyncSession, user_id: int) -> str | None:
    """Предупреждение до обмена: сколько будет стоить следующая передача."""
    points = transfer_points(await transfers_in_window(session, user_id) + 1)
    if not points:
        return None
    return f"⚠️ Это будет уже не первая передача обязанности за {TRANSFER_WINDOW.days} дней — −{points}."
