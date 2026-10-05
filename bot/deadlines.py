"""Когда что происходит с игрой (локальное время команды)."""

from datetime import datetime, timedelta, timezone

from bot.config import config
from bot.models import Game


def created_local(game: Game) -> datetime:
    return game.created_at.replace(tzinfo=timezone.utc).astimezone(config.tz).replace(tzinfo=None)


def to_utc(local: datetime) -> datetime:
    return local.replace(tzinfo=config.tz).astimezone(timezone.utc).replace(tzinfo=None)


def distribute_at(game: Game) -> datetime | None:
    """Когда автоматически распределять обязанности.

    Обычно за AUTO_DISTRIBUTE_HOURS до начала. Если игру создали позже этого
    момента — перед личным напоминанием, чтобы игроки успели отметиться.
    """
    if config.auto_distribute_hours <= 0:
        return None
    created = created_local(game)
    for hours in (config.auto_distribute_hours, config.personal_reminder_hours):
        moment = game.starts_at - timedelta(hours=hours)
        if moment > created:
            return moment
    return None  # создана совсем впритык — распределит администратор


def rsvp_deadline(game: Game) -> datetime:
    """До какого момента нужно ответить на опрос — потом минус.

    Это момент автораспределения; если оно выключено — за PERSONAL_REMINDER_HOURS до начала.
    Ручное распределение срок не меняет: минусы начисляются только в этот момент.
    """
    return distribute_at(game) or game.starts_at - timedelta(hours=config.personal_reminder_hours)


def rsvp_reminder_at(game: Game) -> datetime | None:
    """Когда напомнить молчащим. None — если игру создали так поздно, что напоминать некогда."""
    moment = rsvp_deadline(game) - timedelta(hours=config.rsvp_reminder_hours)
    return moment if moment > created_local(game) else None


MIN_RSVP_WINDOW = timedelta(hours=1)


def penalties_enabled(game: Game) -> bool:
    """Минусы только если на ответ было хотя бы MIN_RSVP_WINDOW."""
    return rsvp_deadline(game) - created_local(game) >= MIN_RSVP_WINDOW
