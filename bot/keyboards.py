from datetime import datetime, timedelta

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup, WebAppInfo
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot import texts
from bot.models import Game, GameStatus, Rsvp


def main_menu(is_admin: bool) -> ReplyKeyboardMarkup:
    if is_admin:
        rows = [
            [texts.BTN_CREATE, texts.BTN_CURRENT],
            [texts.BTN_PARTICIPANTS, texts.BTN_DISTRIBUTE],
            [texts.BTN_EDIT, texts.BTN_CANCEL],
            [texts.BTN_PLAYERS, texts.BTN_STATS],
            [texts.BTN_PROFILE, texts.BTN_WEB],
        ]
    else:
        rows = [[texts.BTN_CURRENT, texts.BTN_SWAP], [texts.BTN_STATS, texts.BTN_PROFILE], [texts.BTN_WEB]]
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=t) for t in row] for row in rows], resize_keyboard=True
    )


def app_button(game_id: int | None = None, text: str = "📱 Открыть приложение") -> InlineKeyboardButton | None:
    """Кнопка Mini App (только в личке). Нет публичного адреса (локальный запуск) — нет кнопки."""
    from bot.config import config

    if not config.public_url:
        return None
    url = f"{config.public_url}/app" + (f"?game={game_id}" if game_id else "")
    return InlineKeyboardButton(text=text, web_app=WebAppInfo(url=url))


def with_web_button(markup: InlineKeyboardMarkup | None, user) -> InlineKeyboardMarkup | None:
    """Кнопка «🌐 Открыть на сайте» — личная ссылка для браузера (если бот на Vercel)."""
    from bot.weblink import web_link

    link = web_link(user)
    if link is None:
        return markup
    rows = list(markup.inline_keyboard) if markup else []
    return InlineKeyboardMarkup(inline_keyboard=[*rows, [InlineKeyboardButton(text="🌐 Открыть на сайте", url=link)]])


def with_app_button(markup: InlineKeyboardMarkup | None, game_id: int | None = None) -> InlineKeyboardMarkup | None:
    button = app_button(game_id)
    if button is None:
        return markup
    rows = list(markup.inline_keyboard) if markup else []
    return InlineKeyboardMarkup(inline_keyboard=[*rows, [button]])


def car_choice(prefix: str = "car") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=texts.CAR_YES, callback_data=f"{prefix}:1"),
                InlineKeyboardButton(text=texts.CAR_NO, callback_data=f"{prefix}:0"),
            ]
        ]
    )


def profile_actions() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✏️ Изменить имя", callback_data="prof:name")],
            [InlineKeyboardButton(text="🚗 Изменить наличие машины", callback_data="prof:car")],
            [InlineKeyboardButton(text="🌐 Открыть в браузере", callback_data="prof:web")],
        ]
    )


def rsvp(game: Game) -> InlineKeyboardMarkup | None:
    if game.status not in GameStatus.ACTIVE:
        return None
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=texts.RSVP_LABELS[s], callback_data=f"rsvp:{game.id}:{s}")
                for s in (Rsvp.YES, Rsvp.NO, Rsvp.MAYBE)
            ]
        ]
    )


def swap_link(game: Game, bot_username: str) -> InlineKeyboardMarkup | None:
    if game.status != GameStatus.DISTRIBUTED:
        return None
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=texts.BTN_SWAP, url=f"https://t.me/{bot_username}?start=swap_{game.id}")]
        ]
    )


def game_admin(game: Game) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text=texts.BTN_PARTICIPANTS, callback_data=f"adm:parts:{game.id}")
    b.button(text="🎯 Распределить", callback_data=f"adm:dist:{game.id}")
    b.button(text=texts.BTN_EDIT, callback_data=f"adm:edit:{game.id}")
    b.button(text=texts.BTN_CANCEL, callback_data=f"adm:cancel:{game.id}")
    b.adjust(2)
    return b.as_markup()


def game_picker(games: list[Game], action: str) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for g in games:
        b.button(text=texts.game_header(g), callback_data=f"adm:{action}:{g.id}")
    b.adjust(1)
    return b.as_markup()


def confirm(yes_data: str, yes_text: str = "✅ Да", no_text: str = "↩️ Нет") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=yes_text, callback_data=yes_data),
                InlineKeyboardButton(text=no_text, callback_data="noop"),
            ]
        ]
    )


# ------------------------------------------------------------ создание игры


def date_choice(now: datetime, days: int = 8) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for i in range(days):
        d = now + timedelta(days=i)
        label = {0: "Сегодня", 1: "Завтра"}.get(i, texts.fmt_date(d, weekday=True))
        b.button(text=label, callback_data=f"newdate:{d:%Y-%m-%d}")
    b.adjust(2)
    return b.as_markup()


def time_choice() -> InlineKeyboardMarkup:
    """С 13:00 до 24:00 каждые полчаса (24:00 — полночь, конец выбранного дня)."""
    b = InlineKeyboardBuilder()
    for minutes in range(13 * 60, 24 * 60 + 1, 30):
        label = f"{minutes // 60:02d}:{minutes % 60:02d}"
        b.button(text=label, callback_data=f"newtime:{minutes}")
    b.adjust(4)
    return b.as_markup()


def kind_choice() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=texts.KIND_TITLES["game"], callback_data="newkind:game"),
                InlineKeyboardButton(text=texts.KIND_TITLES["training"], callback_data="newkind:training"),
            ]
        ]
    )


def skip(data: str = "newloc:skip") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⏭ Пропустить", callback_data=data)]])


def publish_confirm() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Опубликовать", callback_data="newgame:publish"),
                InlineKeyboardButton(text="❌ Отмена", callback_data="newgame:abort"),
            ]
        ]
    )
