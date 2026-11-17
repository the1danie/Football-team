"""Результат игры: счёт (вносит админ), голы и голевые пасы (каждый себе сам, админ — за любого).

Только для «Игры» (у тренировки результата нет). Голов и пасов у команды не больше, чем забили по счёту.
"""

from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot import texts
from bot.config import config
from bot.models import Game, GameParticipant, GameStat, GameStatus, Rsvp, User

SELF_EDIT_DAYS = 7  # сколько дней после игры игрок может сам отметить гол/пас
MAX_SCORE = 50


class ResultError(ValueError):
    pass


def has_result(game: Game) -> bool:
    return game.kind == "game" and game.status not in GameStatus.NOT_PLAYED and game.status != GameStatus.PLANNED


def started(game: Game) -> bool:
    return config.now() >= game.starts_at


def outcome(game: Game) -> str | None:
    if game.score_us is None or game.score_them is None:
        return None
    return "победа" if game.score_us > game.score_them else "поражение" if game.score_us < game.score_them else "ничья"


async def was_there(session: AsyncSession, game: Game, user: User) -> bool:
    p = await session.get(GameParticipant, (game.id, user.id))
    return p is not None and p.status == Rsvp.YES and p.attended is not False


async def stats(session: AsyncSession, game_id: int) -> list[GameStat]:
    return list((await session.scalars(
        select(GameStat).where(GameStat.game_id == game_id, (GameStat.goals > 0) | (GameStat.assists > 0))
    )).all())


async def set_score(session: AsyncSession, game: Game, us: object, them: object) -> None:
    if not has_result(game):
        raise ResultError("Счёт — только для игры (не тренировки).")
    if not started(game):
        raise ResultError("Счёт можно внести, когда игра началась.")
    us, them = int(us), int(them)
    if not (0 <= us <= MAX_SCORE and 0 <= them <= MAX_SCORE):
        raise ResultError("Неверный счёт.")
    rows = await stats(session, game.id)
    if sum(r.goals for r in rows) > us:
        raise ResultError(f"Игроки уже отметили {sum(r.goals for r in rows)} голов — больше, чем {us}. "
                          "Сначала поправьте голы.")
    game.score_us, game.score_them = us, them
    await session.flush()


async def set_stat(session: AsyncSession, game: Game, target: User, goals: object, assists: object,
                   by_admin: bool) -> GameStat:
    if not has_result(game):
        raise ResultError("Голы и пасы — только для игры (не тренировки).")
    if not started(game):
        raise ResultError("Отметить гол можно, когда игра началась.")
    goals, assists = max(0, min(MAX_SCORE, int(goals))), max(0, min(MAX_SCORE, int(assists)))
    if not by_admin:
        if not await was_there(session, game, target):
            raise ResultError("Отметить гол или пас может тот, кто был на игре.")
        if config.now() > game.starts_at + timedelta(days=SELF_EDIT_DAYS):
            raise ResultError(f"Прошло больше {SELF_EDIT_DAYS} дней — исправить может админ.")
    row = await session.scalar(select(GameStat).where(GameStat.game_id == game.id, GameStat.user_id == target.id))
    others = [r for r in await stats(session, game.id) if r.user_id != target.id]
    if game.score_us is not None:
        if sum(r.goals for r in others) + goals > game.score_us:
            raise ResultError(f"Команда забила {game.score_us} — голов отмечено уже "
                              f"{sum(r.goals for r in others)}. Если счёт другой — попросите админа поправить.")
        if sum(r.assists for r in others) + assists > game.score_us:
            raise ResultError(f"Голевых пасов не может быть больше, чем голов ({game.score_us}).")
    if row is None:
        row = GameStat(game_id=game.id, user_id=target.id, goals=goals, assists=assists)
        session.add(row)
    else:
        row.goals, row.assists = goals, assists
    await session.flush()
    return row


async def view(session: AsyncSession, game: Game, me: User, is_admin: bool) -> dict | None:
    if not has_result(game) or not started(game):
        return None
    rows = await stats(session, game.id)
    users = {u.id: u for u in (await session.scalars(select(User).where(User.id.in_([r.user_id for r in rows])))).all()} if rows else {}
    mine = next((r for r in rows if r.user_id == me.id), None)
    there = await was_there(session, game, me)
    return {
        "us": game.score_us, "them": game.score_them, "outcome": outcome(game),
        "scorers": [{"id": r.user_id, "name": users[r.user_id].name, "goals": r.goals, "assists": r.assists}
                    for r in sorted(rows, key=lambda r: (-r.goals, -r.assists)) if r.user_id in users],
        "my": {"goals": mine.goals if mine else 0, "assists": mine.assists if mine else 0},
        "can_self": there and config.now() <= game.starts_at + timedelta(days=SELF_EDIT_DAYS),
        "can_admin": is_admin,
    }


def summary_line(game: Game, rows: list[GameStat], names: dict[int, str]) -> list[str]:
    lines = []
    if game.score_us is not None:
        lines.append(f"Счёт: {game.score_us}:{game.score_them} — {outcome(game)}")
    goals = [f"{names[r.user_id]}" + (f" {r.goals}" if r.goals > 1 else "") for r in sorted(rows, key=lambda r: -r.goals) if r.goals]
    assists = [f"{names[r.user_id]}" + (f" {r.assists}" if r.assists > 1 else "") for r in sorted(rows, key=lambda r: -r.assists) if r.assists]
    if goals:
        lines.append("⚽ Голы: " + ", ".join(goals))
    if assists:
        lines.append("🅰️ Пасы: " + ", ".join(assists))
    return lines


async def whatsapp_text(session: AsyncSession, game: Game) -> str:
    rows = await stats(session, game.id)
    names = {u.id: u.name for u in (await session.scalars(select(User).where(User.id.in_([r.user_id for r in rows])))).all()} if rows else {}
    head = f"*{texts.kind_title(game)} — {texts.fmt_date(game.starts_at, weekday=True)}*"
    return "\n".join([head, ""] + summary_line(game, rows, names))
