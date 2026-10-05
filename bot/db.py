from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from uuid import uuid4

from sqlalchemy import Boolean, inspect, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from bot.models import Base, Duty, Setting

DEFAULT_DUTIES = [
    # code, emoji, name, action, requires_car, sort_order, phase
    ("balls", "⚽", "Мячи", "забрать мячи и привезти на следующую", True, 10, "after"),
    ("water", "💧", "Вода", "принести воду", False, 20, "before"),
    ("bibs", "👕", "Манишки", "забрать манишки и принести на следующую", False, 30, "after"),
    ("laundry", "🧺", "Стирка манишек", "забрать и постирать манишки", False, 40, "after"),
]
DUTY_PHASES_KEY = "duty_phases_v1"


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


def _add_missing_columns(conn) -> None:
    """Мини-миграция: create_all не добавляет новые колонки в уже существующие таблицы."""
    inspector = inspect(conn)
    existing_tables = set(inspector.get_table_names())
    for table in Base.metadata.sorted_tables:
        if table.name not in existing_tables:
            continue
        have = {c["name"] for c in inspector.get_columns(table.name)}
        for column in table.columns:
            if column.name in have:
                continue
            ddl = column.type.compile(dialect=conn.dialect)
            default = column.server_default.arg if column.server_default is not None else None
            if default is not None and isinstance(column.type, Boolean):
                default = ("TRUE" if default == "1" else "FALSE") if conn.dialect.name == "postgresql" else default
            elif isinstance(default, str):
                default = "'" + default.replace("'", "''") + "'"
            sql = f'ALTER TABLE {table.name} ADD COLUMN "{column.name}" {ddl}'
            if default is not None:
                sql += f" DEFAULT {default}"
            conn.execute(text(sql))


async def init_db(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(_add_missing_columns)

    async with make_sessionmaker(engine)() as session:
        existing = set((await session.scalars(select(Duty.code).where(Duty.code.is_not(None)))).all())
        for code, emoji, name, action, requires_car, order, phase in DEFAULT_DUTIES:
            if code not in existing:
                session.add(
                    Duty(
                        code=code,
                        emoji=emoji,
                        name=name,
                        action=action,
                        requires_car=requires_car,
                        sort_order=order,
                        phase=phase,
                    )
                )
        # Один раз: стандартные обязанности «после тренировки» и их формулировки (для уже работающих баз).
        if await session.get(Setting, DUTY_PHASES_KEY) is None:
            defaults = {d[0]: d for d in DEFAULT_DUTIES}
            for duty in (await session.scalars(select(Duty).where(Duty.code.in_(defaults)))).all():
                duty.phase = defaults[duty.code][6]
                duty.action = defaults[duty.code][3]
            session.add(Setting(key=DUTY_PHASES_KEY, value="1"))
        await session.commit()
