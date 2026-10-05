import os
from dataclasses import dataclass, field
from datetime import datetime
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv()


def _int_list(raw: str) -> list[int]:
    return [int(x) for x in raw.replace(";", ",").split(",") if x.strip()]


def _float(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip()
    return float(raw) if raw else default


@dataclass
class Config:
    bot_token: str = field(default_factory=lambda: os.getenv("BOT_TOKEN", ""))
    database_url: str = field(
        default_factory=lambda: os.getenv("DATABASE_URL", "sqlite+aiosqlite:///football.db")
    )
    admin_ids: list[int] = field(default_factory=lambda: _int_list(os.getenv("ADMIN_IDS", "")))
    # Чат команды можно задать здесь или командой /bindchat в самом чате.
    group_chat_id: int | None = field(
        default_factory=lambda: int(os.getenv("GROUP_CHAT_ID")) if os.getenv("GROUP_CHAT_ID") else None
    )
    timezone: str = field(default_factory=lambda: os.getenv("TZ_NAME", "Asia/Almaty"))
    # За сколько часов до начала автоматически распределить обязанности (0 — только вручную).
    auto_distribute_hours: float = field(default_factory=lambda: _float("AUTO_DISTRIBUTE_HOURS", 5))
    # Личное напоминание ответственным.
    personal_reminder_hours: float = field(default_factory=lambda: _float("PERSONAL_REMINDER_HOURS", 3))
    # Напоминание в общий чат.
    group_reminder_hours: float = field(default_factory=lambda: _float("GROUP_REMINDER_HOURS", 2))
    # Через сколько часов после начала игра считается завершённой.
    finish_after_hours: float = field(default_factory=lambda: _float("FINISH_AFTER_HOURS", 3))

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    def now(self) -> datetime:
        """Текущее локальное время команды (naive) — в нём же хранятся даты игр."""
        return datetime.now(self.tz).replace(tzinfo=None)

    def is_admin(self, telegram_id: int) -> bool:
        return telegram_id in self.admin_ids


config = Config()
