import random
from datetime import datetime, timedelta

import pytest

from bot.db import init_db, make_engine, make_sessionmaker
from bot.models import User
from bot.services import games as svc


@pytest.fixture
async def session(tmp_path):
    engine = make_engine(f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")
    await init_db(engine)
    async with make_sessionmaker(engine)() as s:
        yield s
    await engine.dispose()


@pytest.fixture
def rng():
    return random.Random(42)


_tg = iter(range(1000, 100000))


async def make_user(session, name: str, has_car: bool = False) -> User:
    user = await svc.get_or_create_user(session, next(_tg), name)
    user.has_car = has_car
    user.profile_completed = True
    await session.flush()
    return user


async def make_game(session, days: int = 1, hour: int = 20):
    start = (datetime.now() + timedelta(days=days)).replace(hour=hour, minute=0, second=0, microsecond=0)
    return await svc.create_game(session, "game", start, "Стадион", None)


async def duties_by_code(session):
    return {d.code: d for d in await svc.active_duties(session)}
