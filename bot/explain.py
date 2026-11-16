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

    def who(moment):
        """Кто участвовал в распределении в этот момент, а кто нет и почему."""
        candidates: list[User] = []
        others: list[dict] = []
        for uid, p in parts.items():
            u = users.get(uid)
            if u is None or u.is_staff:
                continue
            changed_after = p.updated_at is not None and p.updated_at > moment
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
            others.append({"user_id": u.id, "name": u.name, "reason": reason})
        return candidates, others

    everyone = [u.id for u in users.values()]
    per_user: dict[int, dict[int, int]] = {}
    for a in (await session.scalars(
        select(Assignment).join(Game, Assignment.game_id == Game.id).where(
            Assignment.status == AssignmentStatus.ACTIVE, Assignment.user_id.in_(everyone),
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
    first_others, first_count = None, 0
    result = []
    for duty_id in sorted({a.duty_id for a in rows}, key=lambda i: (duties[i].sort_order, i)):
        duty = duties[duty_id]
        # Момент распределения именно этой обязанности: «до» — при закрытии сбора, «после» — после тренировки.
        moment = next(a.created_at for a in rows if a.duty_id == duty_id)
        candidates, others = who(moment)
        if first_others is None or moment == at:
            first_others, first_count = others, len(candidates)
        debt = await _debt_at(session, game, moment, [u.id for u in candidates]) if candidates else {}
        # Уже есть обязанность на этой игре от более раннего распределения (например, вода) — он в последнюю очередь.
        busy_now = {a.user_id for a in rows if a.created_at < moment and a.duty_id != duty_id
                    and a.status in (AssignmentStatus.ACTIVE, AssignmentStatus.FAILED)}
        ranking = []
        for u in candidates:
            this = per_user.get(u.id, {}).get(duty_id, 0)
            total = sum(per_user.get(u.id, {}).values())
            minus = debt.get(u.id, 0)
            busy = u.id in busy_last
            surprise = bool(parts.get(u.id) and parts[u.id].came_unannounced)
            score = this * DUTY_WEIGHT + total + (RECENT_PENALTY if busy else 0) - (minus + surprise) * weight
            parts_txt = [f"делал это {this}×{DUTY_WEIGHT}", f"всего обязанностей {total}"]
            if busy:
                parts_txt.append(f"был занят на прошлой игре +{RECENT_PENALTY}")
            if minus:
                parts_txt.append(f"минусы {minus}×−{weight:g}")
            if surprise:
                parts_txt.append(f"пришёл без «Буду» −{weight:g}")
            if u.id in busy_now:
                parts_txt.append("уже есть обязанность на этой игре — в последнюю очередь")
            ranking.append({
                "user_id": u.id, "name": u.name, "score": round(score, 1), "breakdown": " + ".join(parts_txt),
                "no_car": duty.requires_car and not u.has_car, "minus": minus, "busy": u.id in busy_now,
            })
        # Кто получил — первым среди равных: при равных очках бот бросает жребий.
        got = {a.user_id for a in rows if a.duty_id == duty_id and a.status == AssignmentStatus.ACTIVE}
        ranking.sort(key=lambda r: (r["no_car"], r["busy"], r["score"], r["user_id"] not in got))
        history = [
            {"user_id": a.user_id, "name": a.user.name, "time": _local(a.created_at),
             "status": STATUS_WORDS.get(a.status, a.status), "active": a.status == AssignmentStatus.ACTIVE}
            for a in rows if a.duty_id == duty_id
        ]
        result.append({"emoji": duty.emoji, "name": duty.name, "car": duty.requires_car, "at": _local(moment),
                       "count": len(candidates), "ranking": ranking, "history": history,
                       "others": others if moment != at else []})
    return {"at": _local(at), "duties": result, "others": first_others or [], "candidates": first_count,
            "weight": weight, "game": texts.game_header(game)}


def _pts(n) -> str:
    n = round(n, 1)
    a = abs(n)
    word = "очко" if a % 10 == 1 and a % 100 != 11 else "очка" if 2 <= a % 10 <= 4 and not 12 <= a % 100 <= 14 else "очков"
    return f"{n:g} {word}"


def personal_text(data: dict, user_id: int) -> str:
    """«Почему так» для бота: почему тебе / почему не тебе — коротко, как в приложении."""
    lines = [f"🧮 <b>Почему так распределилось</b>", texts.h(data["game"]), ""]
    got = next((d for d in data["duties"] if any(x["active"] and x["user_id"] == user_id for x in d["history"])), None)
    mine = []
    for d in data["duties"]:
        pos = next((i for i, r in enumerate(d["ranking"]) if r["user_id"] == user_id), None)
        if pos is None:
            continue
        r = d["ranking"][pos]
        if d is got:
            tie = next((x for x in d["ranking"] if x["user_id"] != user_id and not x["no_car"] and not x["busy"]
                        and x["score"] == r["score"]), None)
            mine.append(f"{d['emoji']} {d['name']} — тебе: ты был {pos + 1}-м из {len(d['ranking'])}, "
                        f"у тебя {_pts(r['score'])} ({texts.h(r['breakdown'])})."
                        + (f" Поровну с {texts.h(tie['name'])} — выпал жребий." if tie else ""))
        elif r["no_car"]:
            mine.append(f"{d['emoji']} {d['name']} — не тебе: нужна машина.")
        elif got is None:
            w = next((x for x in d["ranking"] if any(y["active"] and y["user_id"] == x["user_id"] for y in d["history"])), None)
            why = ("её ещё никому не назначили" if w is None
                   else f"у вас с {texts.h(w['name'])} поровну ({_pts(r['score'])}) — решил жребий" if w["score"] == r["score"]
                   else f"у {texts.h(w['name'])} меньше очков: {w['score']:g} против твоих {r['score']:g}" if w["score"] < r["score"]
                   else f"{texts.h(w['name'])} назначили вручную или вы поменялись")
            mine.append(f"{d['emoji']} {d['name']} — не тебе: {why}.")
    out = next((o for o in data["others"] if o["user_id"] == user_id), None)
    if out:
        lines.append(f"🙋 Ты не участвовал: {texts.h(out['reason'])}.")
    elif mine:
        lines += ["🙋 " + ("Почему тебе" if got else "А ты?")] + mine
    else:
        lines.append("🙋 Тебя не было среди «Буду» на момент распределения.")
    lines += ["", "<b>Как бот выбирает</b> — у кого меньше очков, тот получает раньше:",
              "➕3 за каждый раз, когда уже делал эту обязанность", "➕1 за каждую обязанность вообще",
              "➕2 если был занят на прошлой игре", f"➖{data['weight']:g} за каждый минус в долге",
              "Поровну — жребий. Мячи — только с машиной. Одному — одна обязанность."]
    for d in data["duties"]:
        queue = ", ".join(("<b>ты</b>" if r["user_id"] == user_id else texts.h(r["name"])) + f" {r['score']:g}"
                          for r in d["ranking"] if not r["no_car"])
        if queue:
            lines.append(f"\n{d['emoji']} {d['name']}: {queue}")
    return "\n".join(lines)
