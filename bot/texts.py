"""Тексты сообщений бота."""

from datetime import datetime
from html import escape

from bot.models import Assignment, Duty, Game, GameStatus, Rsvp, User

MONTHS = [
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
]
WEEKDAYS = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]

KIND_TITLES = {"game": "⚽ Игра", "training": "🏃 Тренировка"}
KIND_WORDS = {"game": "игра", "training": "тренировка"}

RSVP_LABELS = {Rsvp.YES: "✅ Буду", Rsvp.NO: "❌ Не буду", Rsvp.MAYBE: "🤔 Пока не знаю"}

# Кнопки главного меню
BTN_CREATE = "➕ Создать игру"
BTN_CURRENT = "📅 Текущая игра"
BTN_PARTICIPANTS = "👥 Участники"
BTN_DISTRIBUTE = "🎯 Распределить обязанности"
BTN_EDIT = "✏️ Изменить назначение"
BTN_CANCEL = "❌ Отменить игру"
BTN_STATS = "📊 Статистика"
BTN_PROFILE = "👤 Мой профиль"
BTN_SWAP = "🔄 Поменяться"

CAR_YES = "🚗 Есть машина"
CAR_NO = "🚶 Нет машины"


def h(text: str) -> str:
    return escape(text, quote=False)


def mention(user: User) -> str:
    return f'<a href="tg://user?id={user.telegram_id}">{h(user.name)}</a>'


def fmt_date(dt: datetime, weekday: bool = False) -> str:
    s = f"{dt.day} {MONTHS[dt.month - 1]}"
    return f"{s} ({WEEKDAYS[dt.weekday()]})" if weekday else s


def fmt_time(dt: datetime) -> str:
    return dt.strftime("%H:%M")


def day_word(dt: datetime, now: datetime) -> str:
    delta = (dt.date() - now.date()).days
    if delta == 0:
        return "Сегодня"
    if delta == 1:
        return "Завтра"
    return fmt_date(dt).capitalize()


def kind_title(game: Game) -> str:
    return KIND_TITLES.get(game.kind, KIND_TITLES["game"])


def game_header(game: Game) -> str:
    return f"{kind_title(game)} — {fmt_date(game.starts_at)}, {fmt_time(game.starts_at)}"


def people_word(n: int) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return "человек"
    if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14):
        return "человека"
    return "человек"


def announce_text(game: Game, by_status: dict[str, list[User]]) -> str:
    lines = [f"<b>{kind_title(game)}</b>"]
    if game.status == GameStatus.CANCELLED:
        lines = [f"<b>❌ ОТМЕНЕНА — {KIND_WORDS.get(game.kind, 'игра')}</b>", f"<s>{kind_title(game)}</s>"]
    lines.append(f"📅 {fmt_date(game.starts_at, weekday=True)}")
    lines.append(f"🕗 {fmt_time(game.starts_at)}")
    if game.location:
        lines.append(f"📍 {h(game.location)}")

    if game.status == GameStatus.CANCELLED:
        return "\n".join(lines)

    lines += ["", "Кто будет?", ""]
    for status in (Rsvp.YES, Rsvp.MAYBE, Rsvp.NO):
        users = by_status.get(status, [])
        names = ", ".join(h(u.name) + (" 🚗" if u.has_car and status == Rsvp.YES else "") for u in users)
        lines.append(f"{RSVP_LABELS[status]} ({len(users)})" + (f": {names}" if names else ""))
    yes = len(by_status.get(Rsvp.YES, []))
    lines += ["", f"<b>Подтвердили: {yes} {people_word(yes)}</b>"]
    if game.status == GameStatus.FINISHED:
        lines.append("\n🏁 Игра прошла.")
    return "\n".join(lines)


def duties_block(assignments: list[Assignment], unassigned: list[Duty], with_mentions: bool = False) -> list[str]:
    lines = []
    for a in assignments:
        name = mention(a.user) if with_mentions else h(a.user.name)
        lines.append(f"{a.duty.emoji} {h(a.duty.name)} — {name}")
    for d in unassigned:
        lines.append(f"{d.emoji} {h(d.name)} — ⚠️ <i>не назначено</i>")
    return lines


def summary_text(
    game: Game, yes_count: int, assignments: list[Assignment], unassigned: list[Duty]
) -> str:
    lines = [f"<b>{game_header(game)}</b>"]
    if game.location:
        lines.append(f"📍 {h(game.location)}")
    if game.status == GameStatus.CANCELLED:
        lines += ["", "❌ Игра отменена, обязанности сняты."]
        return "\n".join(lines)
    lines += ["", f"Участников: {yes_count}", "", "<b>Обязанности</b>", ""]
    lines += duties_block(assignments, unassigned)
    if any(d.requires_car for d in unassigned):
        lines += ["", no_car_warning()]
    if game.status == GameStatus.DISTRIBUTED:
        lines += ["", "Если не сможете выполнить обязанность — нажмите «Поменяться»."]
    return "\n".join(lines)


def no_car_warning() -> str:
    return "⚠️ Среди участников нет игрока с машиной.\nНазначьте ответственного за мячи вручную."


def unassigned_warning(duties: list[Duty]) -> str:
    car = [d for d in duties if d.requires_car]
    other = [d for d in duties if not d.requires_car]
    parts = []
    if car:
        names = ", ".join(d.title for d in car)
        parts.append(
            f"⚠️ Среди участников нет игрока с машиной ({names}).\n"
            "Назначьте ответственного вручную: ✏️ Изменить назначение."
        )
    if other:
        parts.append("⚠️ Не хватило участников для: " + ", ".join(d.title for d in other))
    return "\n\n".join(parts)


def personal_reminder(game: Game, now: datetime, duties: list[Duty]) -> str:
    word = KIND_WORDS.get(game.kind, "игра")
    lines = [f"{day_word(game.starts_at, now)} {word} в {fmt_time(game.starts_at)}."]
    if game.location:
        lines.append(f"📍 {h(game.location)}")
    lines += ["", "Твоя обязанность:" if len(duties) == 1 else "Твои обязанности:"]
    lines += [f"{d.emoji} {h(d.action)}." for d in duties]
    return "\n".join(lines)


def group_reminder(game: Game, now: datetime, assignments: list[Assignment]) -> str:
    word = KIND_WORDS.get(game.kind, "игра")
    emoji = kind_title(game).split()[0]
    lines = [f"{emoji} <b>{day_word(game.starts_at, now)} {word} в {fmt_time(game.starts_at)}.</b>"]
    if game.location:
        lines.append(f"📍 {h(game.location)}")
    lines += ["", "Ответственные:"]
    lines += [f"{a.duty.emoji} {mention(a.user)} — {h(a.duty.name.lower())}" for a in assignments]
    return "\n".join(lines)


def profile_text(user: User, minuses: int = 0) -> str:
    car = CAR_YES if user.has_car else CAR_NO
    text = f"<b>👤 Мой профиль</b>\n\nИмя: {h(user.name)}\nМашина: {car}"
    if minuses:
        text += (
            f"\n\n⚠️ Минусы: {minuses} — за неответы на опросы. Пока они есть, обязанности "
            "достаются тебе первым; каждая выполненная обязанность списывает один минус."
        )
    return text


# ---------------------------------------------------------------- опросы и минусы


def minus_word(n: int) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return "минус"
    if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14):
        return "минуса"
    return "минусов"


def until(deadline: datetime, now: datetime) -> str:
    return f"{day_word(deadline, now).lower()} до {fmt_time(deadline)}"


def game_lines(game: Game) -> list[str]:
    lines = [f"<b>{kind_title(game)}</b>", f"📅 {fmt_date(game.starts_at, weekday=True)}", f"🕗 {fmt_time(game.starts_at)}"]
    if game.location:
        lines.append(f"📍 {h(game.location)}")
    return lines


def poll_invite(game: Game, deadline: datetime, now: datetime, penalty_points: int) -> str:
    lines = ["📣 <b>Открыт сбор на игру — кто придёт?</b>", ""] + game_lines(game)
    lines += ["", f"Отметься {until(deadline, now)} кнопками ниже."]
    if penalty_points > 0:
        lines.append(f"Кто не ответит — получит {penalty_points} {minus_word(penalty_points)} ⚠️")
    return "\n".join(lines)


def rsvp_nudge(game: Game, deadline: datetime, now: datetime, penalty_points: int) -> str:
    lines = ["⏰ <b>Ты ещё не ответил, придёшь ли</b>", ""] + game_lines(game)
    lines += ["", f"Сбор закрывается {until(deadline, now)}."]
    if penalty_points > 0:
        lines.append(f"Не ответишь — {penalty_points} {minus_word(penalty_points)}. Достаточно нажать «❌ Не буду».")
    return "\n".join(lines)


def group_nudge(game: Game, users: list[User], deadline: datetime, now: datetime) -> str:
    names = ", ".join(mention(u) for u in users)
    return (
        f"⏰ <b>{game_header(game)}</b>\n\n"
        f"Ещё не отметились ({len(users)}): {names}\n\n"
        f"Сбор закрывается {until(deadline, now)} — потом минус. Кнопки — в сообщении об игре выше."
    )


def penalty_dm(game: Game, points: int, total: int, limit: int) -> str:
    lines = [
        f"⚠️ Ты не ответил на опрос: {game_header(game)}.",
        f"Начислено: −{points}. Всего: {total} {minus_word(total)}.",
        "",
        "Пока есть минусы, обязанности достаются тебе в первую очередь. "
        "Каждая выполненная обязанность списывает один минус.",
    ]
    if limit and total >= limit:
        lines += ["", f"❗ У тебя {total} {minus_word(total)} — администраторы получили уведомление."]
    return "\n".join(lines)


def penalty_group(game: Game, users: list[User], points: int) -> str:
    names = ", ".join(h(u.name) for u in users)
    return f"🙈 Не ответили на опрос ({game_header(game)}): {names} — по −{points}."


def penalty_limit_admin(rows: list[tuple[User, int]], limit: int) -> str:
    lines = [f"❗ Игроки, у которых {limit} и больше минусов за неответы на опросы:", ""]
    lines += [f"• {h(u.name)} — {n} {minus_word(n)}" for u, n in rows]
    lines += ["", "Подробности и снятие минусов: /penalties"]
    return "\n".join(lines)


def penalty_redeemed(game: Game, count: int, left: int) -> str:
    text = f"✅ Обязанность на «{game_header(game)}» выполнена — списано минусов: {count}."
    return text + (f" Осталось: {left}." if left else " Минусов больше нет 👍")
