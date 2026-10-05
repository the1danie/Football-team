"""Работа с играми, участниками, обязанностями и историей."""

import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import config
from bot.models import (
    Assignment,
    AssignmentStatus,
    Duty,
    DutyPhase,
    Game,
    GameParticipant,
    GameStatus,
    Penalty,
    PenaltyStatus,
    Rsvp,
    Schedule,
    Setting,
    SwapRequest,
    SwapStatus,
    User,
    UserStatus,
)
from bot.services.distribution import Candidate, DutySpec, distribute, pick

# ---------------------------------------------------------------- пользователи


async def get_user_by_tg(session: AsyncSession, telegram_id: int) -> User | None:
    return await session.scalar(select(User).where(User.telegram_id == telegram_id))


async def get_or_create_user(
    session: AsyncSession, telegram_id: int, default_name: str, username: str | None = None
) -> User:
    user = await get_user_by_tg(session, telegram_id)
    if user is None:
        user = User(
            telegram_id=telegram_id,
            name=(default_name or "Игрок")[:64],
            has_car=False,
            username=username,
            # Админов и (если подтверждение выключено) всех — сразу в команду.
            status=UserStatus.APPROVED
            if config.is_admin(telegram_id) or not config.require_approval
            else UserStatus.PENDING,
        )
        session.add(user)
        await session.flush()
    return user


async def name_from_telegram(session: AsyncSession, tg_user) -> str:
    """Имя для команды из профиля Telegram: имя, а если такое уже есть у другого игрока — имя и фамилия."""
    existing = await get_user_by_tg(session, tg_user.id)
    if existing is not None and existing.name:
        return existing.name  # уже создан (например, нажал кнопку в чате) — имя не меняем
    first = (tg_user.first_name or "").strip()
    full = " ".join(p for p in (first, (tg_user.last_name or "").strip()) if p)
    if not first:
        return full[:64]
    taken = await session.scalar(
        select(func.count()).select_from(User).where(User.name == first, User.telegram_id != tg_user.id)
    )
    return (full if taken else first)[:64]


async def all_users(session: AsyncSession) -> list[User]:
    return list((await session.scalars(select(User).order_by(User.name))).all())


async def roster(session: AsyncSession) -> list[User]:
    """Состав команды: заполнили профиль и не исключены админом."""
    rows = await session.scalars(
        select(User)
        .where(User.profile_completed.is_(True), User.is_active.is_(True), User.status == UserStatus.APPROVED)
        .order_by(User.name)
    )
    return list(rows.all())


# ---------------------------------------------------------------- настройки


async def get_setting(session: AsyncSession, key: str) -> str | None:
    row = await session.get(Setting, key)
    return row.value if row else None


async def set_setting(session: AsyncSession, key: str, value: str) -> None:
    row = await session.get(Setting, key)
    if row is None:
        session.add(Setting(key=key, value=value))
    else:
        row.value = value


# ---------------------------------------------------------------- игры


async def create_game(
    session: AsyncSession,
    kind: str,
    starts_at: datetime,
    location: str | None,
    created_by: User | None,
    min_players: int | None = None,
    schedule_id: int | None = None,
) -> Game:
    game = Game(
        kind=kind,
        starts_at=starts_at,
        location=location,
        status=GameStatus.OPEN,
        created_by=created_by.id if created_by else None,
        min_players=min_players or None,
        schedule_id=schedule_id,
        scheduled_for=starts_at if schedule_id else None,
    )
    session.add(game)
    await session.flush()
    return game


async def get_game(session: AsyncSession, game_id: int) -> Game | None:
    return await session.get(Game, game_id)


async def upcoming_games(session: AsyncSession, now: datetime, grace_hours: float = 3) -> list[Game]:
    """Игры, которые ещё не прошли (с запасом grace_hours после начала)."""
    rows = await session.scalars(
        select(Game)
        .where(Game.status.in_(GameStatus.ACTIVE), Game.starts_at > now - timedelta(hours=grace_hours))
        .order_by(Game.starts_at)
    )
    return list(rows.all())


async def cancel_game(session: AsyncSession, game: Game) -> list[Assignment]:
    """Отменить игру. Возвращает снятые назначения (чтобы уведомить людей)."""
    assignments = await active_assignments(session, game.id)
    for a in assignments:
        a.status = AssignmentStatus.CANCELLED
    await _expire_swaps(session, game.id)
    game.status = GameStatus.CANCELLED
    await session.flush()
    return assignments


# ---------------------------------------------------------------- участники


async def participants(session: AsyncSession, game_id: int) -> list[GameParticipant]:
    rows = await session.scalars(
        select(GameParticipant)
        .where(GameParticipant.game_id == game_id)
        .order_by(GameParticipant.updated_at)
    )
    return list(rows.all())


async def participants_by_status(session: AsyncSession, game_id: int) -> dict[str, list[User]]:
    result: dict[str, list[User]] = {s: [] for s in Rsvp.ALL}
    for p in await participants(session, game_id):
        result[p.status].append(p.user)
    return result


async def get_rsvp(session: AsyncSession, game_id: int, user_id: int) -> str | None:
    p = await session.get(GameParticipant, (game_id, user_id))
    return p.status if p else None


# ---------------------------------------------------------------- обязанности


async def active_duties(session: AsyncSession, phase: str | None = None) -> list[Duty]:
    q = select(Duty).where(Duty.is_active.is_(True)).order_by(Duty.sort_order, Duty.id)
    if phase is not None:
        q = q.where(Duty.phase == phase)
    return list((await session.scalars(q)).all())


async def all_duties(session: AsyncSession) -> list[Duty]:
    return list((await session.scalars(select(Duty).order_by(Duty.sort_order, Duty.id))).all())


def phases_open(game: Game) -> set[str]:
    """Какие этапы обязанностей уже распределялись на этой игре."""
    result = set()
    if game.status in (GameStatus.DISTRIBUTED, GameStatus.FINISHED):
        result.add(DutyPhase.BEFORE)
    if game.after_duties_done:
        result.add(DutyPhase.AFTER)
    return result


async def add_duty(session: AsyncSession, emoji: str, name: str, requires_car: bool) -> Duty:
    max_order = await session.scalar(select(func.max(Duty.sort_order))) or 0
    duty = Duty(
        emoji=emoji,
        name=name,
        action=name.lower(),
        requires_car=requires_car,
        sort_order=max_order + 10,
    )
    session.add(duty)
    await session.flush()
    return duty


async def active_assignments(session: AsyncSession, game_id: int) -> list[Assignment]:
    rows = await session.scalars(
        select(Assignment)
        .join(Duty, Assignment.duty_id == Duty.id)
        .where(Assignment.game_id == game_id, Assignment.status == AssignmentStatus.ACTIVE)
        .order_by(Duty.sort_order, Duty.id)
    )
    return list(rows.all())


async def user_assignments(session: AsyncSession, game_id: int, user_id: int) -> list[Assignment]:
    return [a for a in await active_assignments(session, game_id) if a.user_id == user_id]


async def build_candidates(
    session: AsyncSession, game: Game, exclude_user_ids: set[int] | None = None
) -> list[Candidate]:
    """Кандидаты — все, кто отметил «Буду», с их историей обязанностей."""
    exclude_user_ids = exclude_user_ids or set()
    users = [u for u in (await participants_by_status(session, game.id))[Rsvp.YES] if u.id not in exclude_user_ids]
    if not users:
        return []
    ids = [u.id for u in users]

    counts = await session.execute(
        select(Assignment.user_id, Assignment.duty_id, func.count())
        .join(Game, Assignment.game_id == Game.id)
        .where(
            Assignment.status == AssignmentStatus.ACTIVE,
            Game.status != GameStatus.CANCELLED,
            Game.id != game.id,
            Assignment.user_id.in_(ids),
        )
        .group_by(Assignment.user_id, Assignment.duty_id)
    )
    per_user: dict[int, dict[int, int]] = {}
    for user_id, duty_id, cnt in counts.all():
        per_user.setdefault(user_id, {})[duty_id] = cnt

    prev_game_id = await session.scalar(
        select(Game.id)
        .where(
            Game.starts_at < game.starts_at,
            Game.status.in_((GameStatus.DISTRIBUTED, GameStatus.FINISHED)),
            Game.id != game.id,
        )
        .order_by(Game.starts_at.desc())
        .limit(1)
    )
    busy_last: set[int] = set()
    if prev_game_id is not None:
        busy_last = set(
            (
                await session.scalars(
                    select(Assignment.user_id).where(
                        Assignment.game_id == prev_game_id,
                        Assignment.status == AssignmentStatus.ACTIVE,
                    )
                )
            ).all()
        )

    penalties = await open_penalty_points(session, ids)
    return [
        Candidate(
            user_id=u.id,
            has_car=u.has_car,
            duty_counts=per_user.get(u.id, {}),
            total=sum(per_user.get(u.id, {}).values()),
            busy_last_game=u.id in busy_last,
            penalty_bonus=penalties.get(u.id, 0) * config.penalty_priority,
        )
        for u in users
    ]


def _spec(duty: Duty) -> DutySpec:
    return DutySpec(id=duty.id, requires_car=duty.requires_car, sort_order=duty.sort_order)


async def _current_load(session: AsyncSession, game_id: int) -> dict[int, int]:
    load: dict[int, int] = {}
    for a in await active_assignments(session, game_id):
        load[a.user_id] = load.get(a.user_id, 0) + 1
    return load


@dataclass
class DistributionResult:
    assignments: list[Assignment] = field(default_factory=list)
    unassigned: list[Duty] = field(default_factory=list)


async def distribute_game(
    session: AsyncSession, game: Game, rng: random.Random | None = None, phase: str = DutyPhase.BEFORE
) -> DistributionResult:
    """Распределить (или пересчитать заново) обязанности этапа: до тренировки или после."""
    duties = await active_duties(session, phase)
    duty_ids = {d.id for d in duties}
    for a in await active_assignments(session, game.id):
        if a.duty_id in duty_ids:
            a.status = AssignmentStatus.CANCELLED
    await _expire_swaps(session, game.id)
    await session.flush()

    duty_by_id = {d.id: d for d in duties}
    candidates = await build_candidates(session, game)
    users = {u.id: u for u in (await participants_by_status(session, game.id))[Rsvp.YES]}

    mapping, unassigned = distribute(
        [_spec(d) for d in duties], candidates, rng, base_load=await _current_load(session, game.id)
    )

    result = DistributionResult()
    for duty in duties:  # в порядке отображения
        if duty.id in mapping:
            a = Assignment(game_id=game.id, user=users[mapping[duty.id]], duty=duty)
            session.add(a)
            result.assignments.append(a)
    result.unassigned = [duty_by_id[d] for d in unassigned]
    if phase == DutyPhase.AFTER:
        game.after_duties_done = True
    else:
        game.status = GameStatus.DISTRIBUTED
    await session.flush()
    return result


@dataclass
class Reassignment:
    duty: Duty
    old_user: User
    new_user: User | None


async def reassign(
    session: AsyncSession,
    game: Game,
    assignment: Assignment,
    exclude_user_ids: set[int] | None = None,
    rng: random.Random | None = None,
) -> Reassignment:
    """Передать обязанность следующему подходящему игроку."""
    exclude = set(exclude_user_ids or ()) | {assignment.user_id}
    assignment.status = AssignmentStatus.REPLACED
    await _expire_swaps(session, game.id, assignment_id=assignment.id)
    await session.flush()

    candidates = await build_candidates(session, game, exclude_user_ids=exclude)
    chosen = pick(_spec(assignment.duty), candidates, await _current_load(session, game.id), rng)
    new_user = None
    if chosen is not None:
        new_user = await session.get(User, chosen.user_id)
        session.add(Assignment(game_id=game.id, user=new_user, duty=assignment.duty))
        await session.flush()
    return Reassignment(duty=assignment.duty, old_user=assignment.user, new_user=new_user)


async def unassigned_duties(session: AsyncSession, game: Game) -> list[Duty]:
    """Свободные обязанности тех этапов, что уже распределялись."""
    taken = {a.duty_id for a in await active_assignments(session, game.id)}
    open_phases = phases_open(game)
    return [d for d in await active_duties(session) if d.id not in taken and d.phase in open_phases]


async def pending_after_duties(session: AsyncSession, game: Game) -> list[Duty]:
    """Обязанности «после тренировки», которые ещё впереди."""
    if game.after_duties_done or game.status not in GameStatus.ACTIVE:
        return []
    return await active_duties(session, DutyPhase.AFTER)


async def fill_unassigned(
    session: AsyncSession, game: Game, rng: random.Random | None = None
) -> list[Assignment]:
    """Назначить свободные обязанности (например, пришёл игрок с машиной)."""
    if game.status != GameStatus.DISTRIBUTED:
        return []
    created: list[Assignment] = []
    for duty in await unassigned_duties(session, game):
        candidates = await build_candidates(session, game)
        chosen = pick(_spec(duty), candidates, await _current_load(session, game.id), rng)
        if chosen is None:
            continue
        a = Assignment(game_id=game.id, user=await session.get(User, chosen.user_id), duty=duty)
        session.add(a)
        await session.flush()
        created.append(a)
    return created


@dataclass
class RsvpResult:
    old_status: str | None
    new_status: str
    reassigned: list[Reassignment] = field(default_factory=list)
    filled: list[Assignment] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return self.old_status != self.new_status


async def set_rsvp(
    session: AsyncSession, game: Game, user: User, status: str, rng: random.Random | None = None
) -> RsvpResult:
    assert status in Rsvp.ALL
    p = await session.get(GameParticipant, (game.id, user.id))
    old = p.status if p else None
    if p is None:
        session.add(GameParticipant(game_id=game.id, user_id=user.id, status=status, user=user))
    else:
        p.status = status
    await session.flush()

    result = RsvpResult(old_status=old, new_status=status)
    if game.status != GameStatus.DISTRIBUTED or old == status:
        return result

    if status != Rsvp.YES:
        for a in await user_assignments(session, game.id, user.id):
            result.reassigned.append(await reassign(session, game, a, rng=rng))
    else:
        result.filled = await fill_unassigned(session, game, rng)
    return result


async def drop_car_duties(
    session: AsyncSession, user: User, now: datetime
) -> list[tuple[Game, list[Reassignment]]]:
    """Игрок убрал машину из профиля — передать его «машинные» обязанности."""
    result: list[tuple[Game, list[Reassignment]]] = []
    for game in await upcoming_games(session, now, grace_hours=0):
        if game.status != GameStatus.DISTRIBUTED:
            continue
        moved = [
            await reassign(session, game, a)
            for a in await user_assignments(session, game.id, user.id)
            if a.duty.requires_car
        ]
        if moved:
            result.append((game, moved))
    return result


async def set_assignment(
    session: AsyncSession, game: Game, duty: Duty, user: User | None
) -> tuple[User | None, Assignment | None]:
    """Ручное назначение администратором. Возвращает (прежний исполнитель, новое назначение)."""
    old_user = None
    for a in await active_assignments(session, game.id):
        if a.duty_id == duty.id:
            old_user = a.user
            a.status = AssignmentStatus.REPLACED
            await _expire_swaps(session, game.id, assignment_id=a.id)
    new = None
    if user is not None:
        new = Assignment(game_id=game.id, user=user, duty=duty)
        session.add(new)
    await session.flush()
    return old_user, new


# ---------------------------------------------------------------- обмены


async def swap_targets(
    session: AsyncSession, game: Game, assignment: Assignment
) -> list[tuple[User, Assignment | None]]:
    """С кем можно поменяться: другие участники «Буду» с учётом машины."""
    me = assignment.user
    by_user: dict[int, Assignment] = {}
    for a in await active_assignments(session, game.id):
        by_user.setdefault(a.user_id, a)

    targets: list[tuple[User, Assignment | None]] = []
    for user in (await participants_by_status(session, game.id))[Rsvp.YES]:
        if user.id == me.id:
            continue
        their = by_user.get(user.id)
        if assignment.duty.requires_car and not user.has_car:
            continue
        if their is not None and their.duty.requires_car and not me.has_car:
            continue
        targets.append((user, their))
    return targets


async def create_swap(
    session: AsyncSession, game: Game, assignment: Assignment, target: User
) -> SwapRequest:
    their = next(iter(await user_assignments(session, game.id, target.id)), None)
    req = SwapRequest(
        game_id=game.id,
        from_user_id=assignment.user_id,
        to_user_id=target.id,
        from_assignment_id=assignment.id,
        to_assignment_id=their.id if their else None,
    )
    session.add(req)
    await session.flush()
    return req


async def accept_swap(session: AsyncSession, req: SwapRequest) -> str | None:
    """Выполнить обмен. Возвращает текст ошибки или None при успехе."""
    if req.status != SwapStatus.PENDING:
        return "Это предложение уже неактуально."
    game = await session.get(Game, req.game_id)
    if game is None or game.status != GameStatus.DISTRIBUTED:
        req.status = SwapStatus.EXPIRED
        return "Игра уже неактуальна."
    mine = await session.get(Assignment, req.from_assignment_id)
    theirs = await session.get(Assignment, req.to_assignment_id) if req.to_assignment_id else None
    if (
        mine is None
        or mine.status != AssignmentStatus.ACTIVE
        or mine.user_id != req.from_user_id
        or (req.to_assignment_id and (theirs is None or theirs.status != AssignmentStatus.ACTIVE or theirs.user_id != req.to_user_id))
    ):
        req.status = SwapStatus.EXPIRED
        return "Назначения уже изменились, обмен невозможен."
    if await get_rsvp(session, game.id, req.to_user_id) != Rsvp.YES:
        req.status = SwapStatus.EXPIRED
        return "Ты не отмечен как участник этой игры."

    from_user = await session.get(User, req.from_user_id)
    to_user = await session.get(User, req.to_user_id)
    mine.user = to_user
    if theirs is not None:
        theirs.user = from_user
    req.status = SwapStatus.ACCEPTED
    await session.flush()
    return None


async def _expire_swaps(session: AsyncSession, game_id: int, assignment_id: int | None = None) -> None:
    q = select(SwapRequest).where(SwapRequest.game_id == game_id, SwapRequest.status == SwapStatus.PENDING)
    for req in (await session.scalars(q)).all():
        if assignment_id is None or assignment_id in (req.from_assignment_id, req.to_assignment_id):
            req.status = SwapStatus.EXPIRED


# ---------------------------------------------------------------- статистика


def _counted():
    return (
        select(Assignment.user_id, Assignment.duty_id, func.count().label("cnt"))
        .join(Game, Assignment.game_id == Game.id)
        .where(Assignment.status == AssignmentStatus.ACTIVE, Game.status != GameStatus.CANCELLED)
        .group_by(Assignment.user_id, Assignment.duty_id)
    )


async def team_stats(session: AsyncSession) -> list[tuple[User, int]]:
    totals: dict[int, int] = {}
    for user_id, _duty_id, cnt in (await session.execute(_counted())).all():
        totals[user_id] = totals.get(user_id, 0) + cnt
    users = await all_users(session)
    rows = [
        (u, totals.get(u.id, 0))
        for u in users
        if (u.profile_completed and u.status == UserStatus.APPROVED) or u.id in totals
    ]
    rows.sort(key=lambda r: (-r[1], r[0].name))
    return rows


async def player_stats(session: AsyncSession, user_id: int) -> list[tuple[Duty, int]]:
    counts = {
        duty_id: cnt
        for uid, duty_id, cnt in (await session.execute(_counted())).all()
        if uid == user_id
    }
    duties = list((await session.scalars(select(Duty).order_by(Duty.sort_order, Duty.id))).all())
    return [(d, counts.get(d.id, 0)) for d in duties if d.is_active or counts.get(d.id)]


# ---------------------------------------------------------------- опросы и минусы


async def non_responders(session: AsyncSession, game: Game) -> list[User]:
    """Игроки состава, которые не нажали ни одну кнопку опроса.

    Тех, кто присоединился к боту уже после публикации игры, не считаем.
    """
    answered = set(
        (await session.scalars(select(GameParticipant.user_id).where(GameParticipant.game_id == game.id))).all()
    )
    return [u for u in await roster(session) if u.id not in answered and u.created_at <= game.created_at]


async def open_penalty_points(session: AsyncSession, user_ids: list[int] | None = None) -> dict[int, int]:
    q = (
        select(Penalty.user_id, func.sum(Penalty.points))
        .where(Penalty.status == PenaltyStatus.OPEN)
        .group_by(Penalty.user_id)
    )
    if user_ids is not None:
        q = q.where(Penalty.user_id.in_(user_ids))
    return {uid: int(total) for uid, total in (await session.execute(q)).all()}


@dataclass
class PenaltyResult:
    user: User
    points: int  # начислено сейчас
    total: int  # всего действующих минусов


async def apply_no_response_penalties(session: AsyncSession, game: Game) -> list[PenaltyResult]:
    """Закрытие сбора: минус каждому, кто так и не ответил. Выполняется один раз на игру."""
    if game.penalties_applied:
        return []
    game.penalties_applied = True
    if config.penalty_points <= 0:
        await session.flush()
        return []
    users = await non_responders(session, game)
    for user in users:
        session.add(Penalty(user_id=user.id, game_id=game.id, points=config.penalty_points))
    await session.flush()
    totals = await open_penalty_points(session, [u.id for u in users])
    return [PenaltyResult(u, config.penalty_points, totals.get(u.id, 0)) for u in users]


async def redeem_penalties(session: AsyncSession, game: Game) -> list[tuple[User, int]]:
    """Игра прошла: каждая выполненная обязанность списывает один (самый старый) минус."""
    redeemed: dict[int, tuple[User, int]] = {}
    for a in await active_assignments(session, game.id):
        penalty = await session.scalar(
            select(Penalty)
            .where(Penalty.user_id == a.user_id, Penalty.status == PenaltyStatus.OPEN)
            .order_by(Penalty.created_at, Penalty.id)
            .limit(1)
        )
        if penalty is None:
            continue
        penalty.status = PenaltyStatus.REDEEMED
        penalty.redeemed_game_id = game.id
        user, n = redeemed.get(a.user_id, (a.user, 0))
        redeemed[a.user_id] = (user, n + 1)
        await session.flush()
    return list(redeemed.values())


async def user_penalties(session: AsyncSession, user_id: int, open_only: bool = True) -> list[Penalty]:
    q = select(Penalty).where(Penalty.user_id == user_id).order_by(Penalty.created_at, Penalty.id)
    if open_only:
        q = q.where(Penalty.status == PenaltyStatus.OPEN)
    return list((await session.scalars(q)).all())


async def cancel_penalty(session: AsyncSession, penalty_id: int) -> Penalty | None:
    penalty = await session.get(Penalty, penalty_id)
    if penalty is None or penalty.status != PenaltyStatus.OPEN:
        return None
    penalty.status = PenaltyStatus.CANCELLED
    await session.flush()
    return penalty


# ---------------------------------------------------------------- управление игроками (админ)


async def players_for_admin(session: AsyncSession) -> list[User]:
    """Все, кто заходил в бота: сначала ждущие подтверждения, потом команда, потом заблокированные."""
    order = {UserStatus.PENDING: 0, UserStatus.APPROVED: 1, UserStatus.BLOCKED: 2}
    users = [u for u in await all_users(session) if u.profile_completed or u.status == UserStatus.PENDING]
    return sorted(users, key=lambda u: (order.get(u.status, 3), u.name.lower()))


async def _leave_upcoming_games(session: AsyncSession, user: User, now: datetime) -> list[tuple[Game, list[Reassignment]]]:
    """Снять игрока со всех будущих игр (его обязанности перейдут другим)."""
    result = []
    for game in await upcoming_games(session, now, grace_hours=0):
        if await get_rsvp(session, game.id, user.id) == Rsvp.YES:
            r = await set_rsvp(session, game, user, Rsvp.NO)
            result.append((game, r.reassigned))
    return result


async def set_user_status(
    session: AsyncSession, user: User, status: str, now: datetime
) -> list[tuple[Game, list[Reassignment]]]:
    user.status = status
    await session.flush()
    if status == UserStatus.BLOCKED:
        return await _leave_upcoming_games(session, user, now)
    return []


async def set_car(
    session: AsyncSession, user: User, has_car: bool, now: datetime, by_admin: bool
) -> list[tuple[Game, list[Reassignment]]]:
    """Изменить наличие машины. Если машины больше нет — «машинные» обязанности уходят другим."""
    had_car = user.has_car
    user.has_car = has_car
    if by_admin:
        user.car_locked = True
    await session.flush()
    if had_car and not has_car:
        return await drop_car_duties(session, user, now)
    return []


# ---------------------------------------------------------------- расписание


async def schedules(session: AsyncSession) -> list[Schedule]:
    rows = await session.scalars(select(Schedule).order_by(Schedule.weekday, Schedule.minutes))
    return list(rows.all())


def next_occurrence(schedule: Schedule, now: datetime) -> datetime:
    """Ближайшее будущее начало по расписанию."""
    day = now.date() + timedelta(days=(schedule.weekday - now.weekday()) % 7)
    start = datetime.combine(day, datetime.min.time()) + timedelta(minutes=schedule.minutes)
    if start <= now:
        start += timedelta(days=7)
    return start


async def due_schedule_games(session: AsyncSession, now: datetime) -> list[tuple[Schedule, datetime]]:
    """Какие игры по расписанию пора создать (открылось окно и игры ещё нет)."""
    result = []
    for schedule in await schedules(session):
        if not schedule.is_active:
            continue
        start = next_occurrence(schedule, now)
        if now < start - timedelta(days=schedule.open_days_before):
            continue
        exists = await session.scalar(
            select(func.count())
            .select_from(Game)
            .where(
                Game.schedule_id == schedule.id,
                or_(Game.scheduled_for == start, and_(Game.scheduled_for.is_(None), Game.starts_at == start)),
            )
        )
        if not exists:
            result.append((schedule, start))
    return result


async def yes_count(session: AsyncSession, game_id: int) -> int:
    return int(
        await session.scalar(
            select(func.count())
            .select_from(GameParticipant)
            .where(GameParticipant.game_id == game_id, GameParticipant.status == Rsvp.YES)
        )
        or 0
    )


# ---------------------------------------------------------------- админы


async def refresh_admins(session: AsyncSession) -> None:
    """Подтянуть из БД админов, назначенных в боте (вызывается на каждый запрос)."""
    rows = await session.scalars(
        select(User.telegram_id).where(User.is_admin.is_(True), User.status == UserStatus.APPROVED)
    )
    config.extra_admin_ids = set(rows.all())
