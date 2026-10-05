from datetime import timedelta

from sqlalchemy import select, text

from bot.config import config
from bot.db import init_db, make_engine, make_sessionmaker
from bot.models import Game, PenaltyStatus, Rsvp, User
from bot.services import games as svc
from tests.conftest import db_url, make_game, make_user


async def test_non_responders(session):
    a = await make_user(session, "А")
    b = await make_user(session, "Б")
    injured = await make_user(session, "Травма")
    injured.is_active = False
    game = await make_game(session)
    late = await make_user(session, "Новичок")  # пришёл в бота после публикации
    late.created_at = game.created_at + timedelta(minutes=1)
    stranger = await svc.get_or_create_user(session, 777, "Без профиля")  # не заполнил профиль
    await svc.set_rsvp(session, game, a, Rsvp.NO)
    assert stranger.profile_completed is False
    assert [u.name for u in await svc.non_responders(session, game)] == [b.name]


async def test_penalties_applied_once_and_counted(session):
    a = await make_user(session, "А")
    await make_user(session, "Б")
    g1 = await make_game(session, days=1)
    g2 = await make_game(session, days=2)
    await svc.set_rsvp(session, g1, a, Rsvp.MAYBE)  # «Пока не знаю» — это ответ, минуса нет

    r1 = await svc.apply_no_response_penalties(session, g1)
    assert [(r.user.name, r.points, r.total) for r in r1] == [("Б", 1, 1)]
    assert await svc.apply_no_response_penalties(session, g1) == []  # повторно — ничего
    r2 = await svc.apply_no_response_penalties(session, g2)
    assert sorted((r.user.name, r.total) for r in r2) == [("А", 1), ("Б", 2)]


async def test_penalty_gives_duty_priority_and_redeems(session, rng):
    slacker = await make_user(session, "Молчун", has_car=True)
    good = [await make_user(session, n, has_car=True) for n in ("А", "Б", "В", "Г")]
    old = await make_game(session, days=1)
    await svc.apply_no_response_penalties(session, old)
    assert (await svc.open_penalty_points(session))[slacker.id] == 1
    # «Хорошие» тоже не ответили в той игре — снимем им минусы, оставим только молчуну.
    for u in good:
        for p in await svc.user_penalties(session, u.id):
            await svc.cancel_penalty(session, p.id)

    # Молчун раньше делал много обязанностей, но минус ставит его первым в очередь.
    game = await make_game(session, days=3)
    for u in [slacker, *good]:
        await svc.set_rsvp(session, game, u, Rsvp.YES)
    history = await make_game(session, days=-10)
    duties = await svc.active_duties(session)
    for d in duties:  # «прошлые» обязанности молчуна
        await svc.set_assignment(session, history, d, slacker)
    history.status = "finished"
    await session.flush()

    candidates = {c.user_id: c for c in await svc.build_candidates(session, game)}
    assert candidates[slacker.id].penalty_bonus == config.penalty_priority
    result = await svc.distribute_game(session, game, rng)
    assert slacker.id in {a.user_id for a in result.assignments}

    redeemed = await svc.redeem_penalties(session, game)
    assert [(u.id, n) for u, n in redeemed] == [(slacker.id, 1)]
    assert await svc.open_penalty_points(session) == {}
    p = (await svc.user_penalties(session, slacker.id, open_only=False))[0]
    assert p.status == PenaltyStatus.REDEEMED and p.redeemed_game_id == game.id


async def test_cancel_penalty(session):
    u = await make_user(session, "А")
    game = await make_game(session)
    await svc.apply_no_response_penalties(session, game)
    p = (await svc.user_penalties(session, u.id))[0]
    assert (await svc.cancel_penalty(session, p.id)).status == PenaltyStatus.CANCELLED
    assert await svc.cancel_penalty(session, p.id) is None
    assert await svc.open_penalty_points(session) == {}


async def test_no_penalties_when_disabled(session, monkeypatch):
    monkeypatch.setattr(config, "penalty_points", 0)
    await make_user(session, "А")
    game = await make_game(session)
    assert await svc.apply_no_response_penalties(session, game) == []
    assert game.penalties_applied


async def test_migration_adds_new_columns_to_old_db(tmp_path):
    """Бот уже работал со старой схемой — новые колонки добавятся при старте."""
    url = await db_url(tmp_path, "old.db")
    engine = make_engine(url)
    await init_db(engine)
    async with engine.begin() as conn:
        await conn.execute(text("INSERT INTO users (telegram_id, name, has_car, profile_completed, created_at) "
                                "VALUES (1, 'Старичок', false, true, CURRENT_TIMESTAMP)"))
        await conn.execute(text("ALTER TABLE users DROP COLUMN is_active"))
        await conn.execute(text("DROP INDEX ix_users_status"))
        for col in ("status", "car_locked", "username"):
            await conn.execute(text(f"ALTER TABLE users DROP COLUMN {col}"))
        await conn.execute(text("ALTER TABLE games DROP COLUMN rsvp_nudge_sent"))
        await conn.execute(text("ALTER TABLE games DROP COLUMN penalties_applied"))
        await conn.execute(text("DROP TABLE penalties"))
    await init_db(engine)
    async with make_sessionmaker(engine)() as s:
        user = await s.scalar(select(User))
        assert user.is_active is True  # существующие игроки — в составе
        assert user.status == "approved"  # и уже подтверждены — доступ не теряют
        assert user.car_locked is False
        game = await svc.create_game(s, "game", config.now() + timedelta(days=1), None, None)
        await s.commit()
        assert (await s.get(Game, game.id)).penalties_applied is False
        assert [u.name for u in await svc.non_responders(s, game)] == ["Старичок"]
    await engine.dispose()
