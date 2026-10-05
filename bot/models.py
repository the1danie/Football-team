from datetime import datetime, timezone

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Integer, String
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


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    name: Mapped[str] = mapped_column(String(64))
    has_car: Mapped[bool] = mapped_column(Boolean, default=False)
    profile_completed: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


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

    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


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


class Setting(Base):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(String(255))
