from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from bot.models import Base, Duty

DEFAULT_DUTIES = [
    # code, emoji, name, action, requires_car, sort_order
    ("balls", "⚽", "Мячи", "привезти мячи", True, 10),
    ("water", "💧", "Вода", "принести воду", False, 20),
    ("bibs", "👕", "Манишки", "принести манишки", False, 30),
    ("laundry", "🧺", "Стирка манишек", "забрать и постирать манишки", False, 40),
]


# Параметры libpq из строки подключения Neon, которых asyncpg не понимает.
_LIBPQ_ONLY = {"sslmode", "channel_binding", "options", "target_session_attrs"}


def normalize_db_url(url: str) -> tuple[str, dict]:
    """Привести строку подключения (в т.ч. из Neon/Vercel) к виду для asyncpg.

    postgres://u:p@host/db?sslmode=require  ->  postgresql+asyncpg://u:p@host/db  + ssl=require
    """
    if url.startswith("postgres://"):
        url = "postgresql://" + url.removeprefix("postgres://")
    if url.startswith("postgresql://"):
        url = "postgresql+asyncpg://" + url.removeprefix("postgresql://")
    if not url.startswith("postgresql+asyncpg://"):
        return url, {}

    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query))
    connect_args: dict = {
        # Совместимость с пулером соединений (PgBouncer у Neon, «-pooler» в адресе).
        "statement_cache_size": 0,
        "prepared_statement_name_func": lambda: f"__asyncpg_{uuid4()}__",
    }
    sslmode = query.get("sslmode") or query.get("ssl")
    if sslmode and sslmode != "disable":
        connect_args["ssl"] = "require"
    query = {k: v for k, v in query.items() if k not in _LIBPQ_ONLY and k != "ssl"}
    return urlunsplit(parts._replace(query=urlencode(query))), connect_args


def make_engine(url: str, serverless: bool = False) -> AsyncEngine:
    """serverless=True — без пула соединений: каждый вызов функции открывает своё."""
    url, connect_args = normalize_db_url(url)
    if url.startswith("sqlite"):
        return create_async_engine(url)
    if serverless:
        return create_async_engine(url, poolclass=NullPool, connect_args=connect_args)
    return create_async_engine(url, pool_pre_ping=True, connect_args=connect_args)


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
