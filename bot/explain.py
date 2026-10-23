"""«Почему так распределилось»: восстановить, кто и с какими очками участвовал в распределении игры.

Считаем по состоянию на момент первого распределения: кто тогда был «Буду», какие минусы были в долге,
сколько раз каждый уже делал обязанности. Ниже — история назначений (пересчёты, замены, передачи).
"""

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot import texts
from bot.config import config
from bot.models import (
    Assignment, AssignmentStatus, Duty, Game, GameParticipant, GameStatus, Penalty, PenaltyStatus, Rsvp, User,
)
from bot.services import games as svc
from bot.services.distribution import DUTY_WEIGHT, RECENT_PENALTY

STATUS_WORDS = {
    AssignmentStatus.ACTIVE: "сейчас", AssignmentStatus.CANCELLED: "пересчитано",
    AssignmentStatus.REPLACED: "заменён", AssignmentStatus.FAILED: "не выполнил",
}
RSVP_WORDS = {Rsvp.YES: "Буду", Rsvp.NO: "Не буду", Rsvp.MAYBE: "Не знаю"}


def _local(utc: datetime) -> str:
    return texts.fmt_time(utc.replace(tzinfo=timezone.utc).astimezone(config.tz).replace(tzinfo=None))


async def _debt_at(session: AsyncSession, game: Game, at: datetime, ids: list[int]) -> dict[int, int]:
    """Минусы в долге на момент at (что потом отработали или закрыли бёрпи — тогда ещё висело)."""
    later_games = set((await session.scalars(
        select(Game.id).where(Game.starts_at >= game.starts_at)
    )).all())
    debt: dict[int, int] = {}
    for p in (await session.scalars(
        select(Penalty).where(Penalty.user_id.in_(ids), Penalty.created_at <= at)
    )).all():
        was_open = (
            p.status == PenaltyStatus.OPEN
            or (p.status == PenaltyStatus.REDEEMED and p.redeemed_game_id in later_games)
            or (p.status == PenaltyStatus.BURPEES and p.burpees_at is not None and p.burpees_at > at)
        )
        if was_open:
            debt[p.user_id] = debt.get(p.user_id, 0) + p.points
    return debt


async def explain(session: AsyncSession, game: Game) -> dict | None:
    rows = (await session.scalars(
        select(Assignment).where(Assignment.game_id == game.id).order_by(Assignment.created_at, Assignment.id)
    )).all()
    if not rows:
        return None
    at = rows[0].created_at  # первое распределение
    duties = {d.id: d for d in (await session.scalars(select(Duty))).all()}

    parts = {p.user_id: p for p in (await session.scalars(
        select(GameParticipant).where(GameParticipant.game_id == game.id)
    )).all()}
    users = {u.id: u for u in await svc.all_users(session)}
    candidates: list[User] = []
    others: list[dict] = []
    for uid, p in parts.items():
        u = users.get(uid)
        if u is None or u.is_staff:
            continue
        changed_after = p.updated_at is not None and p.updated_at > at
        if p.status == Rsvp.YES and not changed_after and p.attended is not False:
            candidates.append(u)
            continue
        if changed_after:
            reason = f"ответ изменён после распределения (в {_local(p.updated_at)}), сейчас «{RSVP_WORDS[p.status]}»"
        elif p.auto_from == "silent":
            reason = "молчал — при закрытии сбора стал «Не буду»"
        elif p.auto_from == "maybe":
            reason = "«Не знаю» — при закрытии сбора стал «Не буду»"
        elif p.attended is False:
            reason = "отмечен «не пришёл»"
        else:
            reason = f"«{RSVP_WORDS[p.status]}»"
        others.append({"name": u.name, "reason": reason})

    ids = [u.id for u in candidates]
    debt = await _debt_at(session, game, at, ids) if ids else {}
    per_user: dict[int, dict[int, int]] = {}
    if ids:
        for a in (await session.scalars(
            select(Assignment).join(Game, Assignment.game_id == Game.id).where(
                Assignment.status == AssignmentStatus.ACTIVE, Assignment.user_id.in_(ids),
                Game.status.not_in(GameStatus.NOT_PLAYED), Game.starts_at < game.starts_at,
            )
        )).all():
            per_user.setdefault(a.user_id, {}).setdefault(a.duty_id, 0)
            per_user[a.user_id][a.duty_id] += 1
    prev_id = await session.scalar(
        select(Game.id).where(Game.starts_at < game.starts_at, Game.status.in_((GameStatus.DISTRIBUTED, GameStatus.FINISHED)))
        .order_by(Game.starts_at.desc()).limit(1)
    )
    busy_last = set((await session.scalars(
        select(Assignment.user_id).where(Assignment.game_id == prev_id, Assignment.status == AssignmentStatus.ACTIVE)
    )).all()) if prev_id else set()

    weight = config.penalty_priority
    result = []
    for duty_id in sorted({a.duty_id for a in rows}, key=lambda i: (duties[i].sort_order, i)):
        duty = duties[duty_id]
        ranking = []
        for u in candidates:
            this = per_user.get(u.id, {}).get(duty_id, 0)
            total = sum(per_user.get(u.id, {}).values())
            minus = debt.get(u.id, 0)
            busy = u.id in busy_last
            score = this * DUTY_WEIGHT + total + (RECENT_PENALTY if busy else 0) - minus * weight
            parts_txt = [f"делал это {this}×{DUTY_WEIGHT}", f"всего обязанностей {total}"]
            if busy:
                parts_txt.append(f"был занят на прошлой игре +{RECENT_PENALTY}")
            if minus:
                parts_txt.append(f"минусы {minus}×−{weight:g}")
            ranking.append({
                "name": u.name, "score": round(score, 1), "breakdown": " + ".join(parts_txt),
                "no_car": duty.requires_car and not u.has_car, "minus": minus,
            })
        ranking.sort(key=lambda r: (r["no_car"], r["score"]))
        history = [
            {"name": a.user.name, "time": _local(a.created_at), "status": STATUS_WORDS.get(a.status, a.status)}
            for a in rows if a.duty_id == duty_id
        ]
        result.append({"emoji": duty.emoji, "name": duty.name, "car": duty.requires_car,
                       "ranking": ranking, "history": history})
    return {"at": _local(at), "duties": result, "others": others, "candidates": len(candidates),
            "weight": weight, "game": texts.game_header(game)}
