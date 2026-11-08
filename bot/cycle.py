"""План на месяц (цикл): задачи тренера, план каждой тренировки, замеры и голы — и прогресс по ним.

Вести план могут тренер (штаб), капитан и админы; видят план и прогресс все в команде.
Тренировки цикла — тренировки из календаря в датах цикла (по порядку: 1-я, 2-я…), матчи — игры вида «Матч».
"""

from datetime import date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot import texts
from bot.config import config
from bot.models import Cycle, CyclePlan, CycleTask, Game, GameGoals, GameStatus, GoalSource, TaskMeasure, User

SOURCE_TITLES = {
    GoalSource.PLAY: "⚽ С игры",
    GoalSource.CORNER: "🚩 Угловой",
    GoalSource.THROW_IN: "🤾 Аут",
    GoalSource.FREE_KICK: "🎯 Штрафной",
    GoalSource.PENALTY: "⚪ Пенальти",
}
MAX_TASKS = 6
MAX_TRAININGS = 20
MAX_COUNT = 99

# Шаблон для первого плана — задачи, с которых команда начинает.
TEMPLATE = {
    "title": "План на месяц",
    "trainings": 8,
    "set_piece_goal": 2,
    "tasks": [
        {"title": "Владение мячом: передвижением доводить атаку до завершения",
         "attempts_label": "атак", "success_label": "до удара"},
        {"title": "Стандарты в голы: по 2 комбинации на угловых, аутах и штрафных",
         "attempts_label": "розыгрышей", "success_label": "голов"},
        {"title": "Защита против пятерых",
         "attempts_label": "атак пятерых", "success_label": "остановили"},
    ],
}


class CycleError(Exception):
    pass


def can_plan(user: User | None, is_admin: bool, staff: str | None) -> bool:
    """Кто ведёт план: админы, тренер (штаб) и капитан."""
    return bool(is_admin or staff or (user is not None and user.is_captain))


async def current(session: AsyncSession, today: date | None = None) -> Cycle | None:
    """Действующий цикл: тот, что идёт сегодня; если нет — ближайший будущий; если нет — последний прошедший."""
    today = today or config.now().date()
    cycles = (await session.scalars(select(Cycle).order_by(Cycle.starts_on, Cycle.id))).all()
    if not cycles:
        return None
    running = [c for c in cycles if c.starts_on.date() <= today <= c.ends_on.date()]
    if running:
        return running[-1]
    future = [c for c in cycles if c.starts_on.date() > today]
    return future[0] if future else cycles[-1]


def _clean(text: object, limit: int) -> str:
    return " ".join(str(text or "").split())[:limit]


def _count(value: object) -> int:
    return max(0, min(MAX_COUNT, int(value or 0)))


async def save(session: AsyncSession, body: dict) -> Cycle:
    """Создать или изменить цикл. Задачи сопоставляются по id: убранные удаляются вместе с замерами."""
    cycle = await session.get(Cycle, int(body["id"])) if body.get("id") else None
    starts = date.fromisoformat(str(body.get("starts_on")))
    ends = date.fromisoformat(str(body.get("ends_on")))
    if ends < starts:
        raise CycleError("Конец плана раньше начала.")
    if (ends - starts).days > 92:
        raise CycleError("План — не длиннее 3 месяцев.")
    title = _clean(body.get("title"), 100) or TEMPLATE["title"]
    trainings = max(1, min(MAX_TRAININGS, int(body.get("trainings") or 8)))
    goal = max(0, min(20, int(body.get("set_piece_goal") or 0)))
    raw_tasks = [t for t in (body.get("tasks") or []) if _clean(t.get("title"), 200)][:MAX_TASKS]
    if not raw_tasks:
        raise CycleError("Добавьте хотя бы одну задачу.")

    if cycle is None:
        cycle = Cycle(title=title, starts_on=datetime.combine(starts, datetime.min.time()),
                      ends_on=datetime.combine(ends, datetime.min.time()), trainings=trainings, set_piece_goal=goal)
        session.add(cycle)
        await session.flush()
        await session.refresh(cycle, ["tasks", "plans"])
    else:
        await session.refresh(cycle, ["tasks", "plans"])
        cycle.title, cycle.trainings, cycle.set_piece_goal = title, trainings, goal
        cycle.starts_on = datetime.combine(starts, datetime.min.time())
        cycle.ends_on = datetime.combine(ends, datetime.min.time())

    old = {t.id: t for t in cycle.tasks}
    keep = []
    for i, raw in enumerate(raw_tasks):
        task = old.get(int(raw.get("id") or 0)) or CycleTask()
        task.sort_order = i
        task.title = _clean(raw.get("title"), 200)
        task.attempts_label = _clean(raw.get("attempts_label"), 60) or "попыток"
        task.success_label = _clean(raw.get("success_label"), 60) or "удачно"
        keep.append(task)
    cycle.tasks = keep  # убранные задачи удалятся (delete-orphan), их замеры — каскадом

    plans = {int(k): _clean(v, 1000) for k, v in (body.get("plans") or {}).items() if str(k).isdigit()}
    by_number = {p.number: p for p in cycle.plans}
    for number in range(1, trainings + 1):
        if number in plans:
            if number in by_number:
                by_number[number].text = plans[number]
            else:
                cycle.plans.append(CyclePlan(number=number, text=plans[number]))
    cycle.plans = [p for p in cycle.plans if p.number <= trainings]
    await session.flush()
    return cycle


async def delete(session: AsyncSession, cycle_id: int) -> None:
    cycle = await session.get(Cycle, cycle_id)
    if cycle is not None:
        await session.delete(cycle)
        await session.flush()


async def _games(session: AsyncSession, cycle: Cycle) -> list[Game]:
    """Тренировки и матчи в датах цикла (без отменённых), по времени."""
    end = cycle.ends_on + timedelta(days=1)
    return list((await session.scalars(
        select(Game).where(Game.starts_at >= cycle.starts_on, Game.starts_at < end,
                           Game.kind.in_(("training", "match")), Game.status.not_in(GameStatus.NOT_PLAYED))
        .order_by(Game.starts_at, Game.id)
    )).all())


def _plan_text(cycle: Cycle, number: int | None) -> str:
    if not number:
        return ""
    return next((p.text for p in cycle.plans if p.number == number), "")


async def set_measure(session: AsyncSession, game: Game, task_id: int, attempts: object, success: object) -> None:
    task = await session.get(CycleTask, task_id)
    if task is None:
        raise CycleError("Задача не найдена.")
    attempts, success = _count(attempts), _count(success)
    if success > attempts:
        raise CycleError("Удачных не может быть больше, чем попыток.")
    row = await session.scalar(select(TaskMeasure).where(TaskMeasure.game_id == game.id, TaskMeasure.task_id == task_id))
    if attempts == 0:
        if row is not None:
            await session.delete(row)
    elif row is None:
        session.add(TaskMeasure(game_id=game.id, task_id=task_id, attempts=attempts, success=success))
    else:
        row.attempts, row.success = attempts, success
    await session.flush()


async def set_goals(session: AsyncSession, game: Game, side: str, source: str, count: object) -> None:
    if side not in ("for", "against") or source not in GoalSource.ALL:
        raise CycleError("Неверный вид гола.")
    count = _count(count)
    row = await session.scalar(select(GameGoals).where(
        GameGoals.game_id == game.id, GameGoals.side == side, GameGoals.source == source))
    if count == 0:
        if row is not None:
            await session.delete(row)
    elif row is None:
        session.add(GameGoals(game_id=game.id, side=side, source=source, count=count))
    else:
        row.count = count
    await session.flush()


def _goals_summary(rows: list[GameGoals]) -> dict:
    by = {"for": {s: 0 for s in GoalSource.ALL}, "against": {s: 0 for s in GoalSource.ALL}}
    for r in rows:
        by[r.side][r.source] += r.count
    return {
        "for": by["for"], "against": by["against"],
        "scored": sum(by["for"].values()), "conceded": sum(by["against"].values()),
        "set_pieces": sum(by["for"][s] for s in GoalSource.SET_PIECES),
        "entered": bool(rows),
    }


async def view(session: AsyncSession, cycle: Cycle | None, now: datetime | None = None) -> dict | None:
    """Всё для вкладки «План»: задачи, план тренировок, занятия цикла с замерами и голами, прогресс."""
    if cycle is None:
        return None
    now = now or config.now()
    await session.refresh(cycle, ["tasks", "plans"])
    games = await _games(session, cycle)
    ids = [g.id for g in games]
    measures = (await session.scalars(select(TaskMeasure).where(TaskMeasure.game_id.in_(ids)))).all() if ids else []
    goals = (await session.scalars(select(GameGoals).where(GameGoals.game_id.in_(ids)))).all() if ids else []

    sessions = []
    n = 0
    for g in games:
        number = None
        if g.kind == "training":
            n += 1
            number = n
        sessions.append({
            "game_id": g.id, "kind": g.kind, "kind_title": texts.KIND_TITLES.get(g.kind, ""),
            "date_label": texts.fmt_date(g.starts_at, weekday=True), "time": texts.fmt_time(g.starts_at),
            "number": number, "past": g.starts_at <= now,
            "plan": _plan_text(cycle, number) if number and number <= cycle.trainings else "",
            "measures": {m.task_id: {"attempts": m.attempts, "success": m.success} for m in measures if m.game_id == g.id},
            "goals": _goals_summary([r for r in goals if r.game_id == g.id]),
        })

    tasks = []
    for t in cycle.tasks:
        points = [{"label": s["date_label"].split(" (")[0], "number": s["number"], **s["measures"][t.id]}
                  for s in sessions if t.id in s["measures"]]
        total_a = sum(p["attempts"] for p in points)
        total_s = sum(p["success"] for p in points)
        tasks.append({
            "id": t.id, "title": t.title, "attempts_label": t.attempts_label, "success_label": t.success_label,
            "points": points, "attempts": total_a, "success": total_s,
            "rate": round(100 * total_s / total_a) if total_a else None,
            "first": round(100 * points[0]["success"] / points[0]["attempts"]) if points else None,
            "last": round(100 * points[-1]["success"] / points[-1]["attempts"]) if points else None,
        })

    matches = [s for s in sessions if s["kind"] == "match" and s["goals"]["entered"]]
    trainings_with_goals = [s for s in sessions if s["kind"] == "training" and s["goals"]["entered"]]
    goal = cycle.set_piece_goal

    def goal_block(rows: list[dict]) -> dict:
        total = sum(s["goals"]["set_pieces"] for s in rows)
        return {
            "count": len(rows), "set_pieces": total,
            "avg": round(total / len(rows), 1) if rows else None,
            "hit": sum(1 for s in rows if goal and s["goals"]["set_pieces"] >= goal),
            "by_source": {src: sum(s["goals"]["for"][src] for s in rows) for src in GoalSource.SET_PIECES},
        }

    done = sum(1 for s in sessions if s["kind"] == "training" and s["past"])
    return {
        "id": cycle.id, "title": cycle.title,
        "starts_on": cycle.starts_on.date().isoformat(), "ends_on": cycle.ends_on.date().isoformat(),
        "dates_label": f"{texts.fmt_date(cycle.starts_on)} — {texts.fmt_date(cycle.ends_on)}",
        "trainings": cycle.trainings, "trainings_done": done, "set_piece_goal": goal,
        "days_left": max(0, (cycle.ends_on.date() - now.date()).days),
        "finished": now.date() > cycle.ends_on.date(),
        "tasks": tasks,
        "plans": {p.number: p.text for p in cycle.plans if p.text},
        "sessions": sessions,
        "matches": goal_block(matches), "scrimmages": goal_block(trainings_with_goals),
        "sources": SOURCE_TITLES, "set_piece_sources": list(GoalSource.SET_PIECES),
    }


async def game_plan(session: AsyncSession, game: Game) -> dict | None:
    """Для карточки тренировки: «Тренировка 3 из 8» и что в плане."""
    if game.kind != "training":
        return None
    day = game.starts_at.date()
    cycle = next((c for c in (await session.scalars(select(Cycle).order_by(Cycle.id.desc()))).all()
                  if c.starts_on.date() <= day <= c.ends_on.date()), None)
    if cycle is None:
        return None
    await session.refresh(cycle, ["plans"])
    number = next((i for i, g in enumerate([x for x in await _games(session, cycle) if x.kind == "training"], 1)
                   if g.id == game.id), None)
    if number is None:
        return None
    return {"number": number, "of": cycle.trainings, "text": _plan_text(cycle, number) if number <= cycle.trainings else ""}


def whatsapp_text(v: dict) -> str:
    """Итог для команды — в WhatsApp/Telegram."""
    lines = [f"*📈 {v['title']}* ({v['dates_label']})",
             f"Тренировок: {v['trainings_done']} из {v['trainings']}"]
    m = v["matches"]
    if v["set_piece_goal"]:
        if m["count"]:
            lines.append(f"Голы со стандартов: {m['avg']} за матч (цель {v['set_piece_goal']}), "
                         f"цель выполнена в {m['hit']} из {m['count']} матчей")
        else:
            lines.append(f"Цель: {v['set_piece_goal']}+ гола со стандартов в каждом матче — матчей пока не было")
    lines.append("")
    for i, t in enumerate(v["tasks"], 1):
        if t["rate"] is None:
            lines.append(f"{i}. {t['title']} — замеров пока нет")
        else:
            trend = ""
            if len(t["points"]) > 1:
                trend = f", было {t['first']}% → стало {t['last']}%"
            lines.append(f"{i}. {t['title']} — {t['success']} из {t['attempts']} ({t['rate']}%{trend})")
    return "\n".join(lines)
