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
        # Интеграция Neon в Vercel задаёт DATABASE_URL (и POSTGRES_URL).
        default_factory=lambda: os.getenv("DATABASE_URL")
        or os.getenv("POSTGRES_URL")
        or "sqlite+aiosqlite:///football.db"
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
    # --- Режим webhook (Vercel)
    # Секрет, который Telegram присылает в заголовке каждого запроса (A-Z, a-z, 0-9, _ и -).
    webhook_secret: str = field(default_factory=lambda: os.getenv("WEBHOOK_SECRET", ""))
    # Секрет для /api/tick (Vercel Cron присылает его как «Authorization: Bearer …»).
    cron_secret: str = field(default_factory=lambda: os.getenv("CRON_SECRET", ""))
    # Публичный адрес, например https://football-bot.vercel.app (по умолчанию — из запроса).
    public_url: str = field(default_factory=lambda: os.getenv("PUBLIC_URL", "").rstrip("/"))
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
