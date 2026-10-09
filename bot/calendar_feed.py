"""Календарь (iCalendar / .ics) для подписки в Google Календаре, iPhone и т. п.

Личная ссылка главного админа: игры и тренировки (с отменами и изменениями) и ближайшие
тренировки по расписанию, которых бот ещё не создал. Календари сами обновляют подписку.
"""

import hashlib
import hmac
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot import texts
from bot.config import config
from bot.deadlines import to_utc
from bot.models import Game, GameStatus, Rsvp
from bot.services import games as svc

GAME_LENGTH = timedelta(hours=2)
SCHEDULE_WEEKS = 8


def _sig(telegram_id: int) -> str:
    key = hashlib.sha256(f"calendar:{config.bot_token}".encode()).digest()
    return hmac.new(key, str(telegram_id).encode(), hashlib.sha256).hexdigest()[:24]


def feed_token(telegram_id: int) -> str:
    return f"{telegram_id}.{_sig(telegram_id)}"


def verify_feed_token(token: str) -> int | None:
    """Ссылка действительна только для главного админа (ADMIN_IDS)."""
    try:
        raw_id, sig = (token or "").split(".")
        telegram_id = int(raw_id)
    except ValueError:
        return None
    if not config.bot_token or not hmac.compare_digest(_sig(telegram_id), sig) or not config.is_owner(telegram_id):
        return None
    return telegram_id


def feed_url(telegram_id: int) -> str | None:
    if not config.public_url:
        return None
    return f"{config.public_url}/api/app?ics={feed_token(telegram_id)}"


def _esc(text: str) -> str:
    return text.replace("\\", "\\\\").replace(";", "\;").replace(",", "\\,").replace("\n", "\\n")


def _fold(line: str) -> str:
    """Строки длиннее 75 байт переносятся (RFC 5545)."""
    out, cur = [], b""
    for ch in line:
        b = ch.encode()
        if len(cur) + len(b) > (75 if not out else 74):
            out.append(cur.decode())
            cur = b""
        cur += b
    out.append(cur.decode())
    return "\r\n ".join(out)


def _dt(local: datetime) -> str:
    return to_utc(local).strftime("%Y%m%dT%H%M%SZ")


def _event(uid: str, start: datetime, summary: str, description: str, location: str | None,
           url: str | None, cancelled: bool, stamp: str) -> list[str]:
    lines = [
        "BEGIN:VEVENT", f"UID:{uid}", f"DTSTAMP:{stamp}", f"DTSTART:{_dt(start)}",
        f"DTEND:{_dt(start + GAME_LENGTH)}", f"SUMMARY:{_esc(summary)}", f"DESCRIPTION:{_esc(description)}",
        f"STATUS:{'CANCELLED' if cancelled else 'CONFIRMED'}",
    ]
    if location:
        lines.append(f"LOCATION:{_esc(location)}")
    if url:
        lines.append(f"URL:{url}")
    if not cancelled:
        gather = config.gather_minutes or 30
        lines += ["BEGIN:VALARM", "ACTION:DISPLAY", f"DESCRIPTION:{_esc(summary)}",
                  f"TRIGGER:-PT{int(gather) + 30}M", "END:VALARM"]
    return lines + ["END:VEVENT"]


async def build(session: AsyncSession) -> str:
    now = config.now()
    stamp = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    site = f"{config.public_url}/app" if config.public_url else None
    games = list((await session.scalars(
        select(Game).where(Game.starts_at >= now - timedelta(days=60), Game.status != GameStatus.DELETED)
        .order_by(Game.starts_at)
    )).all())
    lines = [
        "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//football-team-bot//RU", "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH", f"X-WR-CALNAME:{_esc('Футбол — команда')}", f"X-WR-TIMEZONE:{config.timezone}",
        "REFRESH-INTERVAL;VALUE=DURATION:PT1H", "X-PUBLISHED-TTL:PT1H",
    ]
    taken: set[tuple[int, datetime]] = set()
    for g in games:
        if g.schedule_id:
            taken.add((g.schedule_id, g.scheduled_for or g.starts_at))
        cancelled = g.status == GameStatus.CANCELLED
        by_status = await svc.participants_by_status(session, g.id)
        desc = [texts.kind_title(g) + (" — ОТМЕНЕНА" + (f": {g.cancel_reason}" if g.cancel_reason else "") if cancelled else "")]
        gather = texts.gather_time(g)
        if gather and not cancelled:
            desc.append(f"Сбор в {gather}")
        if not cancelled:
            yes = by_status[Rsvp.YES]
            desc.append(f"Идут: {len(yes)}" + (f" ({', '.join(u.name for u in yes)})" if yes else ""))
            if by_status[Rsvp.NO]:
                desc.append(f"Не будут: {len(by_status[Rsvp.NO])}")
            if g.status == GameStatus.DISTRIBUTED:
                for a in await svc.active_assignments(session, g.id):
                    desc.append(f"{a.duty.emoji} {a.duty.name}: {a.user.name}")
        map_url = texts.map_url(g.location, g.location_url)
        if map_url:
            desc.append(f"{texts.map_label(map_url)}: {map_url}")
        if site:
            desc.append(f"Открыть: {site}?game={g.id}")
        title = ("❌ " if cancelled else "") + texts.kind_title(g) + (f" — {g.location}" if g.location else "")
        lines += _event(f"game-{g.id}@football-team", g.starts_at, title, "\n".join(desc), g.location,
                        f"{site}?game={g.id}" if site else None, cancelled, stamp)

    # Будущие тренировки по расписанию, которые бот ещё не создал (опрос откроется позже).
    for x in await svc.schedules(session):
        if not x.is_active:
            continue
        start = svc.next_occurrence(x, now)
        for _ in range(SCHEDULE_WEEKS):
            if (x.id, start) not in taken:
                title = texts.KIND_TITLES.get(x.kind, "") + (f" — {x.location}" if x.location else "")
                desc = f"По расписанию. Опрос откроется за {x.open_days_before} дн."
                lines += _event(f"schedule-{x.id}-{start:%Y%m%d}@football-team", start, title, desc, x.location,
                                site, False, stamp)
            start += timedelta(days=7)
    lines.append("END:VCALENDAR")
    return "\r\n".join(_fold(line) for line in lines) + "\r\n"
