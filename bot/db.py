from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from uuid import uuid4

from sqlalchemy import Boolean, inspect, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from bot.models import Base, Duty, Setting

DEFAULT_DUTIES = [
    # code, emoji, name, action, requires_car, sort_order, phase, active
    ("balls", "⚽", "Мячи", "забрать мячи и привезти на следующую", True, 10, "after", True),
    ("water", "💧", "Вода", "принести воду", False, 20, "before", True),
    ("bibs", "👕", "Манишки", "забрать манишки, постирать и принести на следующую", False, 30, "after", True),
    # Стирка — та же обязанность, что «Манишки»: кто забрал, тот и стирает. Выключена, можно включить.
    ("laundry", "🧺", "Стирка манишек", "забрать и постирать манишки", False, 40, "after", False),
]
# Один раз: «Стирка манишек» объединяется с «Манишками» (история переносится, обязанность выключается).
MERGE_LAUNDRY_INTO_BIBS = True
MERGE_LAUNDRY_KEY = "merge_laundry_v1"
BURPEES_STATUS_KEY = "burpees_status_v1"
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
        fresh_install = not existing
        for code, emoji, name, action, requires_car, order, phase, active in DEFAULT_DUTIES:
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
                        is_active=active,
                    )
                )
        # Один раз: стандартные обязанности «после тренировки» и их формулировки (для уже работающих баз).
        if await session.get(Setting, DUTY_PHASES_KEY) is None:
            defaults = {d[0]: d for d in DEFAULT_DUTIES}
            for duty in (await session.scalars(select(Duty).where(Duty.code.in_(defaults)))).all():
                duty.phase = defaults[duty.code][6]
                duty.action = defaults[duty.code][3]
            session.add(Setting(key=DUTY_PHASES_KEY, value="1"))
        await session.flush()
        if await session.get(Setting, MERGE_LAUNDRY_KEY) is None:
            if MERGE_LAUNDRY_INTO_BIBS and not fresh_install:
                await _merge_laundry_into_bibs(session)
            session.add(Setting(key=MERGE_LAUNDRY_KEY, value="1"))
        if await session.get(Setting, BURPEES_STATUS_KEY) is None:
            await _mark_burpees_history(session)
            session.add(Setting(key=BURPEES_STATUS_KEY, value="1"))
        await session.commit()


async def _mark_burpees_history(session: AsyncSession) -> None:
    """Один раз: минусы, закрытые бёрпи до появления отдельного статуса, убрать из рейтинга.

    Берём записи журнала «сделал N бёрпи … (было −X)» и помечаем последние списанные минусы
    этого игрока на сумму X (обязанностью списываются самые старые, бёрпи — все остальные)."""
    import re

    from bot.models import AuditLog, Penalty, PenaltyStatus, User

    pattern = re.compile(r"^💪 (.+): сделал \d+ бёрпи — минусы обнулены \(было −(\d+)\)")
    for entry in (await session.scalars(select(AuditLog).where(AuditLog.text.contains("бёрпи — минусы обнулены"))
                                        .order_by(AuditLog.id))).all():
        m = pattern.match(entry.text)
        user = m and (await session.scalars(select(User).where(User.name == m.group(1)))).first()
        if user is None:
            continue
        game_cond = (Penalty.redeemed_game_id == entry.game_id if entry.game_id is not None
                     else Penalty.redeemed_game_id.is_(None))
        left = int(m.group(2))
        for p in (await session.scalars(
            select(Penalty).where(Penalty.user_id == user.id, Penalty.status == PenaltyStatus.REDEEMED,
                                  Penalty.created_at <= entry.created_at, game_cond)
            .order_by(Penalty.created_at.desc(), Penalty.id.desc())
        )).all():
            if left <= 0:
                break
            p.status, left = PenaltyStatus.BURPEES, left - p.points
    await session.flush()


async def _merge_laundry_into_bibs(session: AsyncSession) -> None:
    """Кто забрал манишки, тот и стирает: одна обязанность вместо двух."""
    from bot.models import Assignment, AssignmentStatus, Game, GameStatus

    bibs = await session.scalar(select(Duty).where(Duty.code == "bibs"))
    laundry = await session.scalar(select(Duty).where(Duty.code == "laundry"))
    if bibs is None or laundry is None:
        return
    defaults = {d[0]: d for d in DEFAULT_DUTIES}
    bibs.action = defaults["bibs"][3]
    laundry.is_active = False
    rows = await session.execute(
        select(Assignment, Game.status).join(Game, Assignment.game_id == Game.id).where(Assignment.duty_id == laundry.id)
    )
    for assignment, game_status in rows.all():
        if game_status in GameStatus.ACTIVE and assignment.status == AssignmentStatus.ACTIVE:
            assignment.status = AssignmentStatus.CANCELLED  # иначе на манишки окажутся двое
        else:
            assignment.duty_id = bibs.id  # история: стирал — значит, делал «Манишки»
