import hashlib
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
    # Главные админы (владельцы): только они выдают и снимают права админа другим.
    admin_ids: list[int] = field(default_factory=lambda: _int_list(os.getenv("ADMIN_IDS", "")))
    # Админы, которым права выдали в боте (обновляется из БД на каждый запрос — refresh_admins).
    extra_admin_ids: set[int] = field(default_factory=set)
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
    # Новые игроки попадают в команду только после подтверждения админом (0 — сразу).
    require_approval: bool = field(default_factory=lambda: os.getenv("REQUIRE_APPROVAL", "1") not in ("0", "false"))
    # Не набран минимум игроков: 0 — спросить админов (провести/подождать/отменить), 1 — отменять сразу.
    min_players_auto_cancel: bool = field(
        default_factory=lambda: os.getenv("MIN_PLAYERS_AUTO_CANCEL", "0") not in ("0", "false", "")
    )
    # --- Опросы и минусы
    # Напомнить не ответившим за столько часов до закрытия сбора (= распределения обязанностей).
    rsvp_reminder_hours: float = field(default_factory=lambda: _float("RSVP_REMINDER_HOURS", 3))
    # Сколько минусов за то, что не ответил на опрос (0 — не начислять).
    penalty_points: int = field(default_factory=lambda: int(_float("PENALTY_POINTS", 1)))
    # Минус за неявку: отметил «Буду», а админ отметил «не пришёл» (0 — не начислять).
    no_show_points: int = field(default_factory=lambda: int(_float("NO_SHOW_POINTS", 1)))
    # Через сколько минут после начала прислать админам список «кто пришёл».
    attendance_ask_minutes: float = field(default_factory=lambda: _float("ATTENDANCE_ASK_MINUTES", 15))
    # С какого количества минусов предупреждать админов и игрока.
    penalty_limit: int = field(default_factory=lambda: int(_float("PENALTY_LIMIT", 3)))
    # Насколько минус поднимает игрока в очереди на обязанности (в единицах score).
    penalty_priority: float = field(default_factory=lambda: _float("PENALTY_PRIORITY", 10))
    # Писать в общий чат, кто получил минус (0 — только лично).
    penalty_announce: bool = field(default_factory=lambda: os.getenv("PENALTY_ANNOUNCE", "1") not in ("0", "false", ""))
    # --- Режим webhook (Vercel)
    # Секрет, который Telegram присылает в заголовке каждого запроса (A-Z, a-z, 0-9, _ и -).
    webhook_secret: str = field(default_factory=lambda: os.getenv("WEBHOOK_SECRET", ""))
    # Секрет для /api/tick (Vercel Cron присылает его как «Authorization: Bearer …»).
    cron_secret: str = field(default_factory=lambda: os.getenv("CRON_SECRET", ""))
    # Публичный адрес, например https://football-bot.vercel.app. По умолчанию — основной домен
    # проекта, который Vercel сообщает сам (VERCEL_PROJECT_PRODUCTION_URL), иначе — из запроса.
    public_url: str = field(
        default_factory=lambda: os.getenv("PUBLIC_URL", "").rstrip("/")
        or (f"https://{os.environ['VERCEL_PROJECT_PRODUCTION_URL']}" if os.getenv("VERCEL_PROJECT_PRODUCTION_URL") else "")
    )
    # Сбор за столько минут до начала («сбор в 22:30»); 0 — не показывать.
    gather_minutes: float = field(default_factory=lambda: _float("GATHER_MINUTES", 30))
    # Через сколько минут после начала распределять обязанности «после тренировки».
    after_duties_minutes: float = field(default_factory=lambda: _float("AFTER_DUTIES_MINUTES", 60))
    # Через сколько часов после начала игра считается завершённой.
    finish_after_hours: float = field(default_factory=lambda: _float("FINISH_AFTER_HOURS", 3))

    def __post_init__(self) -> None:
        # WEBHOOK_SECRET можно не задавать — выводим его из токена (Telegram передаёт его в заголовке,
        # снаружи он не виден, а при смене токена меняется сам).
        if not self.webhook_secret and self.bot_token:
            self.webhook_secret = hashlib.sha256(f"webhook:{self.bot_token}".encode()).hexdigest()[:48]

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    def now(self) -> datetime:
        """Текущее локальное время команды (naive) — в нём же хранятся даты игр."""
        return datetime.now(self.tz).replace(tzinfo=None)

    def is_admin(self, telegram_id: int) -> bool:
        return telegram_id in self.admin_ids or telegram_id in self.extra_admin_ids

    def is_owner(self, telegram_id: int) -> bool:
        return telegram_id in self.admin_ids

    @property
    def all_admin_ids(self) -> list[int]:
        """Кому слать заявки и тексты для WhatsApp: главные админы и назначенные."""
        return list(dict.fromkeys([*self.admin_ids, *sorted(self.extra_admin_ids)]))


config = Config()
