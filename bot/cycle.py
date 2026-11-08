"""План на месяц (цикл): задачи тренера, план каждой тренировки, замеры и голы — и прогресс по ним.

Вести план могут тренер (штаб), капитан и админы; видят план и прогресс все в команде.
Тренировки цикла — тренировки из календаря в датах цикла (по порядку: 1-я, 2-я…), матчи — игры вида «Матч».
"""

from datetime import date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot import texts
from bot.config import config
from bot.models import (
    ComboGoals, Cycle, CyclePlan, CycleTask, Game, GameGoals, GameParticipant, GameStatus, GoalSource, Rsvp, SetPiece, TaskMeasure, User, UserStatus,
)

SOURCE_TITLES = {
    GoalSource.PLAY: "⚽ С игры",
    GoalSource.CORNER: "🚩 Угловой",
    GoalSource.THROW_IN: "🦶 Аут",
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


async def _attendance(session: AsyncSession, games: list[Game], now: datetime) -> dict[int, dict]:
    """Кто был на прошедших занятиях: пришёл = отмечен «пришёл» или «Буду» без отметки «не пришёл».
    Пропустил — остальные из состава (в команде на момент занятия, не штаб, в составе)."""
    past = [g for g in games if g.starts_at <= now]
    if not past:
        return {}
    parts = (await session.scalars(
        select(GameParticipant).where(GameParticipant.game_id.in_([g.id for g in past])))).unique().all()
    team = (await session.scalars(select(User).where(
        User.status == UserStatus.APPROVED, User.is_active.is_(True), User.staff_title.is_(None),
        User.profile_completed.is_(True)).order_by(User.name))).all()
    out = {}
    for g in past:
        came_ids = {p.user_id for p in parts if p.game_id == g.id
                    and (p.attended is True or (p.status == Rsvp.YES and p.attended is None))}
        if not came_ids:
            continue  # никто не отмечался — данных о посещении нет
        users = {u.id: u for u in team}
        users.update({p.user_id: p.user for p in parts if p.game_id == g.id and p.user_id in came_ids})
        came = sorted((users[i] for i in came_ids if i in users), key=lambda u: u.name.lower())
        joined_before = g.starts_at + timedelta(days=1)
        missed = [u for u in team if u.id not in came_ids and u.created_at <= joined_before]
        out[g.id] = {"came": [{"id": u.id, "name": u.name} for u in came],
                     "missed": [{"id": u.id, "name": u.name} for u in missed]}
    return out


def recap_text(cycle_view: dict, s: dict, html: bool = True) -> str:
    """Что было на занятии — для тех, кого не было (html=False — простой текст для приложения)."""
    e = texts.h if html else (lambda x: x)
    b = (lambda x: f"<b>{x}</b>") if html else (lambda x: x)
    title = "🏆 Матч" if s["kind"] == "match" else f"🏃 Тренировка {s['number']} из {cycle_view['trainings']}"
    lines = [b(f"📋 Что было без тебя: {title}, {s['date_label']}")]
    if s.get("plan"):
        lines += ["", b("Отрабатывали:"), e(s["plan"])]
    results = []
    for t in cycle_view["tasks"]:
        m = s["measures"].get(t["id"])
        if m:
            results.append(f"• {e(t['title'])}: {m['success']} из {m['attempts']} {e(t['attempts_label'])} "
                           f"— {e(t['success_label'])} ({round(100 * m['success'] / m['attempts'])}%)")
    if results:
        lines += ["", b("Результаты:"), *results]
    g = s["goals"]
    if g["entered"]:
        lines += ["", f"⚽ Счёт {g['scored']}:{g['conceded']}, со стандартов — {g['set_pieces']}."]
    if s.get("combos_scored"):
        lines.append("Забили комбинациями: " + ", ".join(f"«{e(n)}»" for n in s["combos_scored"]) + ".")
    att = s.get("attendance")
    if att:
        lines += ["", f"👥 Были {len(att['came'])}: " + ", ".join(e(u["name"]) for u in att["came"])]
    lines += ["", "Посмотри комбинации и свою роль во вкладке «📈 План» — на следующей тренировке продолжаем."]
    return "\n".join(lines)


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
    attendance = await _attendance(session, games, now)
    combo_rows = (await session.execute(
        select(ComboGoals.game_id, SetPiece.name).join(SetPiece, SetPiece.id == ComboGoals.set_piece_id)
        .where(ComboGoals.game_id.in_(ids), ComboGoals.count > 0).order_by(SetPiece.name))).all() if ids else []

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
            "attendance": attendance.get(g.id),
            "recap_sent": g.recap_sent or 0,
            "combos_scored": [name for gid, name in combo_rows if gid == g.id],
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
    counts = [len(s["attendance"]["came"]) for s in sessions if s["kind"] == "training" and s.get("attendance")]
    return {
        "id": cycle.id, "title": cycle.title,
        "starts_on": cycle.starts_on.date().isoformat(), "ends_on": cycle.ends_on.date().isoformat(),
        "dates_label": f"{texts.fmt_date(cycle.starts_on)} — {texts.fmt_date(cycle.ends_on)}",
        "trainings": cycle.trainings, "trainings_done": done, "set_piece_goal": goal,
        "avg_attendance": round(sum(counts) / len(counts), 1) if counts else None,
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


def whatsapp_text(v: dict, lib: dict | None = None) -> str:
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
    scored = [c for c in (lib or {}).get("items", []) if c["goals_matches"]]
    if scored:
        lines += ["", "Сработали в матчах: " + ", ".join(f"«{c['name']}» — {c['goals_matches']}" for c in scored)]
    return "\n".join(lines)


def with_recaps(v: dict | None) -> dict | None:
    """Текст «что было» к каждому прошедшему занятию — для приложения."""
    if v:
        for s in v["sessions"]:
            if s["past"]:
                s["recap"] = recap_text(v, s, html=False)
    return v


async def send_recap(bot, session: AsyncSession, game: Game) -> int:
    """Разослать пропустившим «что было». Возвращает, скольким дошло."""
    from bot import notifier  # noqa: PLC0415

    c = await current(session, game.starts_at.date())
    v = await view(session, c) if c else None
    s = next((x for x in (v or {}).get("sessions", []) if x["game_id"] == game.id), None)
    if s is None or not s["past"]:
        raise CycleError("Занятие не в плане или ещё не прошло.")
    if not s.get("attendance") or not s["attendance"]["missed"]:
        raise CycleError("Пропустивших нет — рассылать некому.")
    text = recap_text(v, s)
    sent = 0
    for u in s["attendance"]["missed"]:
        user = await session.get(User, u["id"])
        if user is not None and await notifier.send_dm(bot, user, text):
            sent += 1
    game.recap_sent = max(sent, 1)
    await session.flush()
    return sent
