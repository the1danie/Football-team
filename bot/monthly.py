"""Раз в месяц (1-го числа, с MONTHLY_HOUR) — админам и штабу PDF за прошлый месяц: посещаемость и итог.

Если 1-го бот не просыпался — отправит при первом запуске позже в этом месяце. Сразу после установки
(отметки ещё нет) прошлый месяц не шлём: только запоминаем — первый отчёт придёт 1-го числа.
"""

import logging
from datetime import datetime

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import BufferedInputFile
from sqlalchemy.ext.asyncio import AsyncSession

from bot import report_pdf
from bot.config import config
from bot.services import games as svc

log = logging.getLogger(__name__)

MONTHLY_HOUR = 10
SENT_KEY = "monthly_reports_sent"  # значение — «2026-10»: за какой месяц уже отправили
MONTHS_NOM = ["январь", "февраль", "март", "апрель", "май", "июнь",
              "июль", "август", "сентябрь", "октябрь", "ноябрь", "декабрь"]


def previous_month(now: datetime) -> tuple[datetime, datetime]:
    """(начало прошлого месяца, начало этого) — локальное время."""
    end = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    start = (end.replace(year=end.year - 1, month=12) if end.month == 1 else end.replace(month=end.month - 1))
    return start, end


async def recipients(session: AsyncSession) -> list[int]:
    ids = list(dict.fromkeys(
        [*config.all_admin_ids, *(u.telegram_id for u in await svc.staff(session) if not u.is_manual)]
    ))
    return [i for i in ids if i > 0]


async def send_if_due(bot: Bot, session: AsyncSession, now: datetime) -> bool:
    start, end = previous_month(now)
    label = f"{start:%Y-%m}"
    sent = await svc.get_setting(session, SENT_KEY)
    if sent is None:
        await svc.set_setting(session, SENT_KEY, label)
        return False
    if sent == label or (now.day == 1 and now.hour < MONTHLY_HOUR):
        return False
    await svc.set_setting(session, SENT_KEY, label)  # сначала отметка — чтобы не задвоить при сбое отправки
    await session.commit()
    try:
        team = (await bot.me()).first_name or "Команда"
    except Exception:  # noqa: BLE001
        team = "Команда"
    month = f"за {MONTHS_NOM[start.month - 1]} {start.year}"
    attendance, att_name = await report_pdf.attendance_pdf(session, team, end, start, month)
    balance, bal_name = await report_pdf.balance_pdf(session, team, end, start, month)
    for chat_id in await recipients(session):
        try:
            await bot.send_document(chat_id, BufferedInputFile(attendance, att_name),
                                    caption=f"📊 Посещаемость {month}.")
            await bot.send_document(chat_id, BufferedInputFile(balance, bal_name),
                                    caption=f"⚖️ Итог {month} (помощь − минусы) — можно переслать команде.")
        except (TelegramForbiddenError, TelegramBadRequest) as e:
            log.info("Monthly report to %s failed: %s", chat_id, e)
    return True
