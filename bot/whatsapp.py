"""Тексты для группы WhatsApp.

Бот не может писать в WhatsApp сам, поэтому админ получает готовый текст с кнопкой
«📤 Отправить в WhatsApp»: она открывает WhatsApp с этим текстом, остаётся выбрать группу.
Формат — обычный текст, *жирный* по правилам WhatsApp.
"""

import logging
import re
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


# WhatsApp по ссылке «открыть с текстом» показывает эмодзи как «�» — убираем их, оставляя слова.
_EMOJI = re.compile(
    "[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U00002B00-\U00002BFF\U00002300-\U000023FF"
    "\U0000FE00-\U0000FE0F\U0000200D\U000020E3\U0001F1E6-\U0001F1FF]"
)
RSVP_WORDS = {"yes": "Буду", "maybe": "Пока не знаю", "no": "Не буду"}


def clean(text: str) -> str:
    """Текст без эмодзи: только буквы, цифры и обычная пунктуация — WhatsApp показывает их всегда."""
    lines = []
    for line in _EMOJI.sub("", text).split("\n"):
        line = re.sub(r" {2,}", " ", line).strip()
        line = line.replace("« ", "«").replace("( ", "(")
        line = re.sub(r"^\*\s+", "*", line)  # «* Тренировка…*» → «*Тренировка…*»
        lines.append(line)
    return "\n".join(lines)


def share_url(text: str) -> str:
    return "https://wa.me/?text=" + quote(clean(text))


def share_markup(text: str) -> InlineKeyboardMarkup | None:
    url = share_url(text)
    if len(url) > MAX_SHARE_URL:
        return None
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=SHARE_LABEL, url=url)]])


def site_link(game: Game | None = None) -> str | None:
    """Общая ссылка на сайт (без ключа): кто уже входил в браузере — сразу увидит игру."""
    if not config.public_url:
        return None
    return f"{config.public_url}/app" + (f"?game={game.id}" if game else "")


async def game_link(bot: Bot, game: Game) -> str:
    """Где отметиться: бот в Telegram и сайт (для тех, кто без Telegram)."""
    me = await bot.me()
    tg = f"https://t.me/{me.username}?start=game_{game.id}"
    site = site_link(game)
    return f"Telegram: {tg}\nСайт: {site}" if site else tg


def _header(game: Game) -> list[str]:
    lines = [f"*{texts.kind_title(game)} — {texts.fmt_date(game.starts_at, weekday=True)}, {texts.fmt_time(game.starts_at)}*"]
    if texts.gather_time(game):
        lines.append(f"Сбор в {texts.gather_time(game)}")
    if game.location:
        lines.append(f"Место: {game.location}")
    return lines


def announce(game: Game, deadline: datetime, now: datetime, link: str) -> str:
    lines = _header(game) + ["", f"Кто будет? Отметьтесь {texts.until(deadline, now)}:", link]
    if game.min_players:
        lines += ["", f"Нужно минимум {game.min_players} {texts.people_word(game.min_players)}."]
    if config.penalty_points > 0:
        lines += ["", "Кто не ответит — получит минус."]
    return "\n".join(lines)


def status(game: Game, by_status: dict[str, list[User]], link: str) -> str:
    lines = _header(game) + [""]
    for s in (Rsvp.YES, Rsvp.MAYBE, Rsvp.NO):
        users = by_status.get(s, [])
        names = ", ".join(u.name for u in users)
        lines.append(f"{RSVP_WORDS[s]} ({len(users)})" + (f": {names}" if names else ""))
    lines += ["", "Отметиться:", link]
    return "\n".join(lines)


def nudge(game: Game, users: list[User], deadline: datetime, now: datetime, link: str, yes: int = 0) -> str:
    names = ", ".join(u.name for u in users)
    lines = _header(game) + [
        "",
        f"Ещё не отметились ({len(users)}): {names}",
        *([texts.min_players_line(game, yes)] if game.min_players else []),
        "",
        f"Сбор закрывается {texts.until(deadline, now)}" + (" — потом минус." if config.penalty_points > 0 else "."),
        link,
    ]
    return "\n".join(lines)


def duties(
    game: Game, yes_count: int, assignments: list[Assignment], unassigned: list[Duty], link: str,
    pending_after: list[Duty] | None = None, after_at: datetime | None = None,
) -> str:
    lines = _header(game) + ["", f"Участников: {yes_count}", "", "*Обязанности*"]
    lines += [f"{a.duty.name} — {a.user.name}" for a in assignments]
    lines += [f"{d.name} — не назначено" for d in unassigned]
    if pending_after and after_at:
        lines += ["", texts.after_line(pending_after, after_at)]
    lines += ["", "Не сможете — нажмите «Поменяться» в боте или на сайте:", link]
    return "\n".join(lines)


def penalties(game: Game, users: list[User], points: int) -> str:
    names = ", ".join(u.name for u in users)
    return "\n".join(_header(game) + ["", f"Не ответили на опрос: {names} — по −{points}."])


def reminder(game: Game, now: datetime, assignments: list[Assignment]) -> str:
    word = texts.KIND_WORDS.get(game.kind, "игра")
    lines = [f"*{texts.day_word(game.starts_at, now)} {word} в {texts.fmt_time(game.starts_at)}{texts.gather_suffix(game)}*"]
    if game.location:
        lines.append(f"Место: {game.location}")
    lines += ["", "Ответственные:"]
    lines += [f"{a.user.name} — {a.duty.name.lower()}" for a in assignments]
    return "\n".join(lines)


async def send_draft(bot: Bot, text: str, chat_ids: list[int] | None = None, note: str | None = None) -> None:
    """Прислать админам (или в указанные чаты) текст для WhatsApp с кнопкой отправки.

    Текст — отдельным сообщением без лишнего, чтобы его можно было и скопировать целиком.
    """
    text = clean(text)
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


def after_duties(game: Game, assignments: list[Assignment], unassigned: list[Duty]) -> str:
    lines = [f"*После тренировки — {texts.game_header(game)}*", ""]
    lines += [f"{a.duty.name} — {a.user.name}" for a in assignments]
    lines += [f"{d.name} — не назначено" for d in unassigned]
    return "\n".join(lines)


def clean_duties(*args, **kwargs) -> str:
    return clean(duties(*args, **kwargs))


def team_invite(bot_username: str) -> str:
    """Инструкция для группы WhatsApp: как подключиться к боту и сайту."""
    bot = f"https://t.me/{bot_username}"
    return "\n".join([
        "*Как отмечаться на игры и тренировки*",
        "",
        f"1. Откройте бота команды в Telegram: {bot}",
        "2. Нажмите «Старт» — админ подтвердит, что вы из команды.",
        "3. Отмечайтесь «Буду / Не буду» в боте или в приложении (кнопка «Открыть» рядом с полем ввода).",
        "",
        "Не пользуетесь Telegram? Зайдите в бота один раз и нажмите «Сайт» — бот пришлёт личную ссылку, "
        "дальше можно через браузер, без Telegram."
        + (f" Сайт команды: {site_link()}" if site_link() else ""),
        "",
        "Кто не отвечает на опрос до закрытия сбора — получает минус.",
    ])
