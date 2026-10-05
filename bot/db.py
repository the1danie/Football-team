from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from bot.models import Base, Duty

DEFAULT_DUTIES = [
    # code, emoji, name, action, requires_car, sort_order
    ("balls", "⚽", "Мячи", "привезти мячи", True, 10),
    ("water", "💧", "Вода", "принести воду", False, 20),
    ("bibs", "👕", "Манишки", "принести манишки", False, 30),
    ("laundry", "🧺", "Стирка манишек", "забрать и постирать манишки", False, 40),
]


def make_engine(url: str) -> AsyncEngine:
    return create_async_engine(url, pool_pre_ping=not url.startswith("sqlite"))


def make_sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


async def init_db(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with make_sessionmaker(engine)() as session:
        existing = set((await session.scalars(select(Duty.code).where(Duty.code.is_not(None)))).all())
        for code, emoji, name, action, requires_car, order in DEFAULT_DUTIES:
            if code not in existing:
                session.add(
                    Duty(
                        code=code,
                        emoji=emoji,
                        name=name,
                        action=action,
                        requires_car=requires_car,
                        sort_order=order,
                    )
                )
        await session.commit()
