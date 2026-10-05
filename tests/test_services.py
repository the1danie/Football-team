from datetime import timedelta

from bot.models import AssignmentStatus, GameStatus, Rsvp, SwapStatus
from bot.services import games as svc
from tests.conftest import duties_by_code, make_game, make_user


async def _signup(session, game, users, status=Rsvp.YES):
    for u in users:
        await svc.set_rsvp(session, game, u, status)


async def test_distribute_only_yes_and_car_for_balls(session, rng):
    driver = await make_user(session, "Даниял", has_car=True)
    walkers = [await make_user(session, n) for n in ("Арман", "Тимур", "Руслан", "Максим")]
    absent = await make_user(session, "Нурлан", has_car=True)
    game = await make_game(session)
    await _signup(session, game, [driver, *walkers])
    await svc.set_rsvp(session, game, absent, Rsvp.NO)

    result = await svc.distribute_game(session, game, rng)
    duties = await duties_by_code(session)
    by_duty = {a.duty_id: a.user for a in result.assignments}

    assert game.status == GameStatus.DISTRIBUTED
    assert by_duty[duties["balls"].id].id == driver.id
    assert absent.id not in {u.id for u in by_duty.values()}
    assert len({u.id for u in by_duty.values()}) == 4
    assert result.unassigned == []


async def test_no_car_warning_and_late_driver_fills(session, rng):
    walkers = [await make_user(session, n) for n in ("А", "Б", "В", "Г")]
    game = await make_game(session)
    await _signup(session, game, walkers)
    result = await svc.distribute_game(session, game, rng)
    assert [d.code for d in result.unassigned] == ["balls"]

    driver = await make_user(session, "Водитель", has_car=True)
    r = await svc.set_rsvp(session, game, driver, Rsvp.YES, rng)
    assert [a.duty.code for a in r.filled] == ["balls"]
    assert await svc.unassigned_duties(session, game) == []


async def test_player_drops_out_duty_reassigned(session, rng):
    players = [await make_user(session, n, has_car=True) for n in ("А", "Б", "В", "Г", "Д", "Е")]
    game = await make_game(session)
    await _signup(session, game, players)
    await svc.distribute_game(session, game, rng)
    victim_assignment = (await svc.active_assignments(session, game.id))[0]
    victim = victim_assignment.user

    r = await svc.set_rsvp(session, game, victim, Rsvp.NO, rng)
    assert len(r.reassigned) == 1
    re = r.reassigned[0]
    assert re.old_user.id == victim.id and re.new_user is not None and re.new_user.id != victim.id
    # Новый исполнитель — тот, у кого ещё не было обязанности на этой игре.
    active = await svc.active_assignments(session, game.id)
    assert len(active) == 4
    assert len({a.user_id for a in active}) == 4
    assert victim.id not in {a.user_id for a in active}
    assert victim_assignment.status == AssignmentStatus.REPLACED


async def test_drop_without_replacement(session, rng):
    driver = await make_user(session, "Водитель", has_car=True)
    others = [await make_user(session, n) for n in ("А", "Б", "В")]
    game = await make_game(session)
    await _signup(session, game, [driver, *others])
    await svc.distribute_game(session, game, rng)
    r = await svc.set_rsvp(session, game, driver, Rsvp.MAYBE, rng)
    assert [x.new_user for x in r.reassigned] == [None]
    assert [d.code for d in await svc.unassigned_duties(session, game)] == ["balls"]


async def test_swap_accept(session, rng):
    a = await make_user(session, "А", has_car=True)
    b = await make_user(session, "Б", has_car=True)
    game = await make_game(session)
    await _signup(session, game, [a, b])
    await svc.distribute_game(session, game, rng)
    mine = (await svc.user_assignments(session, game.id, a.id))[0]
    theirs = (await svc.user_assignments(session, game.id, b.id))[0]
    my_duty, their_duty = mine.duty_id, theirs.duty_id

    targets = await svc.swap_targets(session, game, mine)
    assert [u.id for u, _ in targets] == [b.id]
    req = await svc.create_swap(session, game, mine, b)
    assert await svc.accept_swap(session, req) is None
    assert req.status == SwapStatus.ACCEPTED
    assert my_duty in {x.duty_id for x in await svc.user_assignments(session, game.id, b.id)}
    assert their_duty in {x.duty_id for x in await svc.user_assignments(session, game.id, a.id)}
    # Повторно принять нельзя.
    assert await svc.accept_swap(session, req) is not None


async def test_swap_respects_car_and_expires(session, rng):
    driver = await make_user(session, "Водитель", has_car=True)
    walker = await make_user(session, "Пешеход")
    extra = [await make_user(session, n) for n in ("В", "Г")]
    game = await make_game(session)
    await _signup(session, game, [driver, walker, *extra])
    await svc.distribute_game(session, game, rng)
    balls = (await svc.user_assignments(session, game.id, driver.id))[0]
    assert balls.duty.code == "balls"
    assert await svc.swap_targets(session, game, balls) == []  # больше никого с машиной

    water = (await svc.user_assignments(session, game.id, walker.id))[0]
    req = await svc.create_swap(session, game, water, extra[0])
    await svc.set_rsvp(session, game, walker, Rsvp.NO, rng)  # инициатор передумал идти
    assert req.status == SwapStatus.EXPIRED
    assert await svc.accept_swap(session, req) is not None


async def test_history_drives_next_distribution_and_stats(session, rng):
    players = [await make_user(session, n, has_car=True) for n in ("А", "Б", "В", "Г")]
    g1 = await make_game(session, days=1)
    await _signup(session, g1, players)
    first = {a.duty_id: a.user_id for a in (await svc.distribute_game(session, g1, rng)).assignments}

    g2 = await make_game(session, days=8)
    await _signup(session, g2, players)
    second = {a.duty_id: a.user_id for a in (await svc.distribute_game(session, g2, rng)).assignments}
    # Никто не получил ту же обязанность второй раз подряд.
    assert all(first[d] != second[d] for d in first)

    stats = dict((u.name, n) for u, n in await svc.team_stats(session))
    assert stats == {"А": 2, "Б": 2, "В": 2, "Г": 2}
    detail = await svc.player_stats(session, players[0].id)
    assert sum(n for _, n in detail) == 2


async def test_cancelled_games_not_counted(session, rng):
    players = [await make_user(session, n, has_car=True) for n in ("А", "Б")]
    game = await make_game(session)
    await _signup(session, game, players)
    await svc.distribute_game(session, game, rng)
    dropped = await svc.cancel_game(session, game)
    assert len(dropped) == 4
    assert all(n == 0 for _, n in await svc.team_stats(session))


async def test_redistribute_replaces_previous(session, rng):
    players = [await make_user(session, n, has_car=True) for n in ("А", "Б", "В", "Г", "Д")]
    game = await make_game(session)
    await _signup(session, game, players)
    await svc.distribute_game(session, game, rng)
    await svc.distribute_game(session, game, rng)
    assert len(await svc.active_assignments(session, game.id)) == 4


async def test_drop_car_duties(session, rng):
    a = await make_user(session, "А", has_car=True)
    b = await make_user(session, "Б", has_car=True)
    game = await make_game(session)
    await _signup(session, game, [a, b])
    await svc.distribute_game(session, game, rng)
    driver = next(x.user for x in await svc.active_assignments(session, game.id) if x.duty.code == "balls")
    driver.has_car = False
    from datetime import datetime

    moved = await svc.drop_car_duties(session, driver, datetime.now())
    assert len(moved) == 1
    new_driver = next(x.user for x in await svc.active_assignments(session, game.id) if x.duty.code == "balls")
    assert new_driver.id != driver.id


async def test_upcoming_games(session):
    from datetime import datetime

    g = await make_game(session)
    assert [x.id for x in await svc.upcoming_games(session, datetime.now())] == [g.id]
    assert await svc.upcoming_games(session, g.starts_at + timedelta(hours=4)) == []
