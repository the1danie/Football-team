from datetime import datetime, timezone

from sqlalchemy import JSON, BigInteger, Boolean, DateTime, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


class GameStatus:
    OPEN = "open"  # идёт сбор участников
    DISTRIBUTED = "distributed"  # обязанности распределены
    FINISHED = "finished"
    CANCELLED = "cancelled"

    ACTIVE = (OPEN, DISTRIBUTED)


class Rsvp:
    YES = "yes"
    NO = "no"
    MAYBE = "maybe"

    ALL = (YES, NO, MAYBE)


class AssignmentStatus:
    ACTIVE = "active"
    REPLACED = "replaced"  # обязанность передана другому (игрок отказался / админ поменял)
    CANCELLED = "cancelled"  # распределение пересчитано или игра отменена


class SwapStatus:
    PENDING = "pending"
    ACCEPTED = "accepted"
    DECLINED = "declined"
    EXPIRED = "expired"


class UserStatus:
    PENDING = "pending"  # ждёт подтверждения админом
    APPROVED = "approved"  # в команде
    BLOCKED = "blocked"  # отклонён / удалён админом


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    name: Mapped[str] = mapped_column(String(64))
    has_car: Mapped[bool] = mapped_column(Boolean, default=False)
    profile_completed: Mapped[bool] = mapped_column(Boolean, default=False)
    # В составе команды: получает опросы, за молчание получает минусы.
    # Админ может временно исключить (травма, уехал).
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="1")
    # Новые игроки ждут подтверждения админом. server_default — для тех, кто был в базе
    # до появления подтверждений: они остаются в команде.
    status: Mapped[str] = mapped_column(
        String(16), default=UserStatus.PENDING, server_default=UserStatus.APPROVED, index=True
    )
    # Машину выставил админ — игрок сам изменить не может.
    car_locked: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    # Права админа, выданные главным админом в боте (главные — в ADMIN_IDS).
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    username: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    @property
    def is_approved(self) -> bool:
        return self.status == UserStatus.APPROVED


class Game(Base):
    __tablename__ = "games"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(String(16), default="game")  # game / training
    # Локальное время команды (см. config.timezone).
    starts_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    location: Mapped[str | None] = mapped_column(String(255), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default=GameStatus.OPEN, index=True)

    chat_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    announce_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    summary_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    personal_reminder_sent: Mapped[bool] = mapped_column(Boolean, default=False)
    group_reminder_sent: Mapped[bool] = mapped_column(Boolean, default=False)
    # Напоминание тем, кто не отметился, и начисление минусов.
    rsvp_nudge_sent: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    penalties_applied: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")

    # Повторяющаяся тренировка: из какого расписания создана.
    schedule_id: Mapped[int | None] = mapped_column(
        ForeignKey("schedules.id", ondelete="SET NULL"), nullable=True, index=True
    )
    # Для какого слота расписания создана (не меняется при переносе — чтобы не создать дубль).
    scheduled_for: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Минимум «Буду» к закрытию сбора, иначе игра отменяется (None/0 — без минимума).
    min_players: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cancel_reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Минимум не набран к закрытию сбора: None — не спрашивали, "asked" — ждём решения админа,
    # "keep" — админ решил проводить. min_recheck_at — когда спросить снова («Подождать»).
    min_decision: Mapped[str | None] = mapped_column(String(16), nullable=True)
    min_recheck_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


WEEKDAYS_FULL = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
WEEKDAYS_SHORT = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
EVERY_WEEKDAY = [
    "каждый понедельник", "каждый вторник", "каждую среду", "каждый четверг",
    "каждую пятницу", "каждую субботу", "каждое воскресенье",
]


class Schedule(Base):
    """Повторяющаяся игра/тренировка: каждую неделю в этот день и время."""

    __tablename__ = "schedules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(String(16), default="training")
    weekday: Mapped[int] = mapped_column(Integer)  # 0 — понедельник
    minutes: Mapped[int] = mapped_column(Integer)  # время начала в минутах от полуночи (до 1440)
    location: Mapped[str | None] = mapped_column(String(255), nullable=True)
    min_players: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # За сколько дней до начала создать игру и разослать опрос.
    open_days_before: Mapped[int] = mapped_column(Integer, default=2)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    @property
    def time_label(self) -> str:
        return f"{self.minutes // 60:02d}:{self.minutes % 60:02d}"


class GameParticipant(Base):
    __tablename__ = "game_participants"

    game_id: Mapped[int] = mapped_column(ForeignKey("games.id", ondelete="CASCADE"), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    status: Mapped[str] = mapped_column(String(8))
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    user: Mapped[User] = relationship(lazy="joined")


class Duty(Base):
    __tablename__ = "duties"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str | None] = mapped_column(String(32), unique=True, nullable=True)
    emoji: Mapped[str] = mapped_column(String(16), default="📌")
    name: Mapped[str] = mapped_column(String(64))
    # Формулировка для напоминания: «привезти мячи».
    action: Mapped[str] = mapped_column(String(128))
    requires_car: Mapped[bool] = mapped_column(Boolean, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=100)

    @property
    def title(self) -> str:
        return f"{self.emoji} {self.name}"


class Assignment(Base):
    __tablename__ = "assignments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    game_id: Mapped[int] = mapped_column(ForeignKey("games.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    duty_id: Mapped[int] = mapped_column(ForeignKey("duties.id", ondelete="CASCADE"))
    status: Mapped[str] = mapped_column(String(16), default=AssignmentStatus.ACTIVE)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    user: Mapped[User] = relationship(lazy="joined")
    duty: Mapped[Duty] = relationship(lazy="joined")


class SwapRequest(Base):
    __tablename__ = "swap_requests"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    game_id: Mapped[int] = mapped_column(ForeignKey("games.id", ondelete="CASCADE"))
    from_user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    to_user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    from_assignment_id: Mapped[int] = mapped_column(ForeignKey("assignments.id", ondelete="CASCADE"))
    # None — у получателя нет обязанности, это просьба забрать обязанность себе.
    to_assignment_id: Mapped[int | None] = mapped_column(
        ForeignKey("assignments.id", ondelete="CASCADE"), nullable=True
    )
    status: Mapped[str] = mapped_column(String(16), default=SwapStatus.PENDING)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class PenaltyStatus:
    OPEN = "open"  # действует
    REDEEMED = "redeemed"  # отработан обязанностью
    CANCELLED = "cancelled"  # снят администратором


class Penalty(Base):
    """Минус за то, что игрок не ответил на опрос до закрытия сбора."""

    __tablename__ = "penalties"
    __table_args__ = (UniqueConstraint("user_id", "game_id", "reason"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    game_id: Mapped[int | None] = mapped_column(ForeignKey("games.id", ondelete="SET NULL"), nullable=True)
    points: Mapped[int] = mapped_column(Integer, default=1)
    reason: Mapped[str] = mapped_column(String(32), default="no_response")
    status: Mapped[str] = mapped_column(String(16), default=PenaltyStatus.OPEN, index=True)
    # Игра, на которой минус отработан обязанностью.
    redeemed_game_id: Mapped[int | None] = mapped_column(
        ForeignKey("games.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    game: Mapped["Game | None"] = relationship(foreign_keys=[game_id], lazy="joined")


class Setting(Base):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(String(255))


class FsmRecord(Base):
    """Состояние пошаговых диалогов (создание игры, ввод имени) — переживает перезапуски."""

    __tablename__ = "fsm_states"

    key: Mapped[str] = mapped_column(String(255), primary_key=True)
    state: Mapped[str | None] = mapped_column(String(255), nullable=True)
    data: Mapped[dict] = mapped_column(JSON, default=dict)
