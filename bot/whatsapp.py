"""Тексты для группы WhatsApp.

Бот не может писать в WhatsApp сам, поэтому админ получает готовый текст с кнопкой
«📤 Отправить в WhatsApp»: она открывает WhatsApp с этим текстом, остаётся выбрать группу.
Формат — обычный текст, *жирный* по правилам WhatsApp.
"""

import logging
from datetime import datetime
from urllib.parse import quote

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from bot import texts
from bot.config import config
from bot.models import Assignment, Duty, Game, Rsvp, User

log = logging.getLogger(__name__)

# Слишком длинную ссылку Telegram может не принять — тогда остаётся скопировать текст вручную.
MAX_SHARE_URL = 2000
SHARE_LABEL = "📤 Отправить в WhatsApp"


def share_url(text: str) -> str:
    return "https://wa.me/?text=" + quote(text)


def share_markup(text: str) -> InlineKeyboardMarkup | None:
    url = share_url(text)
    if len(url) > MAX_SHARE_URL:
        return None
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=SHARE_LABEL, url=url)]])


async def game_link(bot: Bot, game: Game) -> str:
    me = await bot.me()
    return f"https://t.me/{me.username}?start=game_{game.id}"


def _header(game: Game) -> list[str]:
    lines = [f"*{texts.kind_title(game)} — {texts.fmt_date(game.starts_at, weekday=True)}, {texts.fmt_time(game.starts_at)}*"]
    if game.location:
        lines.append(f"📍 {game.location}")
    return lines


def announce(game: Game, deadline: datetime, now: datetime, link: str) -> str:
    lines = _header(game) + ["", f"Кто будет? Отметьтесь в боте {texts.until(deadline, now)}:", link]
    if game.min_players:
        lines += ["", f"Нужно минимум {game.min_players} {texts.people_word(game.min_players)}."]
    if config.penalty_points > 0:
        lines += ["", "Кто не ответит — получит минус ⚠️"]
    return "\n".join(lines)


def status(game: Game, by_status: dict[str, list[User]], link: str) -> str:
    lines = _header(game) + [""]
    for s in (Rsvp.YES, Rsvp.MAYBE, Rsvp.NO):
        users = by_status.get(s, [])
        names = ", ".join(u.name for u in users)
        lines.append(f"{texts.RSVP_LABELS[s]} ({len(users)})" + (f": {names}" if names else ""))
    lines += ["", "Отметиться:", link]
    return "\n".join(lines)


def nudge(game: Game, users: list[User], deadline: datetime, now: datetime, link: str, yes: int = 0) -> str:
    names = ", ".join(u.name for u in users)
    lines = _header(game) + [
        "",
        f"⏰ Ещё не отметились ({len(users)}): {names}",
        *([texts.min_players_line(game, yes)] if game.min_players else []),
        "",
        f"Сбор закрывается {texts.until(deadline, now)}" + (" — потом минус." if config.penalty_points > 0 else "."),
        link,
    ]
    return "\n".join(lines)


def duties(game: Game, yes_count: int, assignments: list[Assignment], unassigned: list[Duty], link: str) -> str:
    lines = _header(game) + ["", f"Участников: {yes_count}", "", "*Обязанности*"]
    lines += [f"{a.duty.emoji} {a.duty.name} — {a.user.name}" for a in assignments]
    lines += [f"{d.emoji} {d.name} — ⚠️ не назначено" for d in unassigned]
    lines += ["", "Не сможете — поменяйтесь в боте («🔄 Поменяться»):", link]
    return "\n".join(lines)


def penalties(game: Game, users: list[User], points: int) -> str:
    names = ", ".join(u.name for u in users)
    return "\n".join(_header(game) + ["", f"🙈 Не ответили на опрос: {names} — по −{points}."])


def reminder(game: Game, now: datetime, assignments: list[Assignment]) -> str:
    word = texts.KIND_WORDS.get(game.kind, "игра")
    lines = [f"*{texts.day_word(game.starts_at, now)} {word} в {texts.fmt_time(game.starts_at)}*"]
    if game.location:
        lines.append(f"📍 {game.location}")
    lines += ["", "Ответственные:"]
    lines += [f"{a.duty.emoji} {a.user.name} — {a.duty.name.lower()}" for a in assignments]
    return "\n".join(lines)


async def send_draft(bot: Bot, text: str, chat_ids: list[int] | None = None, note: str | None = None) -> None:
    """Прислать админам (или в указанные чаты) текст для WhatsApp с кнопкой отправки.

    Текст — отдельным сообщением без лишнего, чтобы его можно было и скопировать целиком.
    """
    markup = share_markup(text)
    for chat_id in chat_ids if chat_ids is not None else config.all_admin_ids:
        try:
            if note:
                await bot.send_message(chat_id, note)
            try:
                await bot.send_message(chat_id, text, reply_markup=markup, parse_mode=None)
            except TelegramBadRequest:
                if markup is None:
                    raise
                # Кнопку не приняли (например, слишком длинная ссылка) — пришлём текст для копирования.
                await bot.send_message(chat_id, text, parse_mode=None)
        except (TelegramForbiddenError, TelegramBadRequest) as e:
            log.info("WhatsApp draft to %s failed: %s", chat_id, e)
