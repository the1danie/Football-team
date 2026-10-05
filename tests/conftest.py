import os
import random
from datetime import datetime, timedelta

import pytest

from bot.db import init_db, make_engine, make_sessionmaker
from bot.models import User
from bot.services import games as svc


from bot.models import Base


async def db_url(tmp_path, name: str = "test.db") -> str:
    """SQLite по умолчанию; TEST_DATABASE_URL=postgresql://… — прогон на PostgreSQL (чистая схема)."""
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        return f"sqlite+aiosqlite:///{tmp_path / name}"
    engine = make_engine(url, serverless=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()
    return url


@pytest.fixture
async def session(tmp_path):
    engine = make_engine(await db_url(tmp_path))
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
    user.status = "approved"
    await session.flush()
    return user


async def make_game(session, days: int = 1, hour: int = 20):
    start = (datetime.now() + timedelta(days=days)).replace(hour=hour, minute=0, second=0, microsecond=0)
    return await svc.create_game(session, "game", start, "Стадион", None)


async def duties_by_code(session):
    return {d.code: d for d in await svc.active_duties(session)}


@pytest.fixture(autouse=True)
def _all_duties_before(request, monkeypatch):
    """Большинство тестов писались, когда все обязанности распределялись сразу («до» игры).
    Тесты с настоящей схемой (вода — до; мячи и манишки — после; стирка объединена с манишками)
    помечены @pytest.mark.real_phases."""
    from bot import db

    if "real_phases" in request.keywords:
        return
    # прежняя схема: 4 обязанности (со стиркой), все — до игры
    monkeypatch.setattr(db, "DEFAULT_DUTIES", [d[:6] + ("before", True) for d in db.DEFAULT_DUTIES])
    monkeypatch.setattr(db, "MERGE_LAUNDRY_INTO_BIBS", False)
