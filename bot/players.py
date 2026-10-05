"""Карточка игрока и список игроков для админа."""

from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy.ext.asyncio import AsyncSession

from bot import texts
from bot.config import config
from bot.models import User, UserStatus
from bot.services import games as svc

STATUS_ICONS = {UserStatus.PENDING: "⏳", UserStatus.APPROVED: "✅", UserStatus.BLOCKED: "⛔"}
STATUS_TITLES = {
    UserStatus.PENDING: "⏳ ждёт подтверждения",
    UserStatus.APPROVED: "✅ в команде",
    UserStatus.BLOCKED: "⛔ заблокирован",
}


def _who(user: User) -> str:
    return texts.h(user.name) + (f" (@{texts.h(user.username)})" if user.username else "")


def role_title(user: User) -> str | None:
    if config.is_owner(user.telegram_id):
        return "👑 главный админ"
    if user.is_admin:
        return "👑 админ"
    return None


async def player_card(
    session: AsyncSession, user: User, viewer_tg: int | None = None
) -> tuple[str, InlineKeyboardMarkup]:
    """viewer_tg — кто смотрит: главному админу видна кнопка выдачи прав."""
    minuses = (await svc.open_penalty_points(session, [user.id])).get(user.id, 0)
    duties = sum(n for _, n in await svc.player_stats(session, user.id))
    car = texts.CAR_YES if user.has_car else texts.CAR_NO
    lines = [
        f"<b>👤 {_who(user)}</b>",
        f'<a href="tg://user?id={user.telegram_id}">написать в Telegram</a>',
        "",
        f"Статус: {STATUS_TITLES.get(user.status, user.status)}" + (f" · {role_title(user)}" if role_title(user) else ""),
        f"Машина: {car}" + (" — <i>закреплено админом</i>" if user.car_locked else " — <i>указал сам</i>"),
    ]
    if user.status == UserStatus.APPROVED:
        lines.append("В составе: " + ("да — получает опросы" if user.is_active else "🚫 нет (травма/уехал) — опросов и минусов нет"))
        lines.append(f"Обязанностей выполнено: {duties}" + (f" · ⚠️ минусов: {minuses}" if minuses else ""))

    b = InlineKeyboardBuilder()
    uid = user.id
    if user.status == UserStatus.PENDING:
        b.button(text="✅ Принять в команду", callback_data=f"pl:approve:{uid}")
        b.button(text="⛔ Отклонить", callback_data=f"pl:block:{uid}")
    b.button(
        text="🚶 Поставить: нет машины" if user.has_car else "🚗 Поставить: есть машина",
        callback_data=f"pl:car:{uid}",
    )
    if user.car_locked:
        b.button(text="🔓 Разрешить игроку менять машину", callback_data=f"pl:unlock:{uid}")
    b.button(text="✏️ Переименовать", callback_data=f"pl:name:{uid}")
    viewer_is_owner = viewer_tg is not None and config.is_owner(viewer_tg)
    if viewer_is_owner and user.status == UserStatus.APPROVED and not config.is_owner(user.telegram_id):
        b.button(
            text="Снять права админа" if user.is_admin else "👑 Сделать админом",
            callback_data=f"pl:{'admin_off' if user.is_admin else 'admin_on'}:{uid}",
        )
    if user.status == UserStatus.APPROVED:
        b.button(
            text="🚫 Убрать из состава (временно)" if user.is_active else "✅ Вернуть в состав",
            callback_data=f"pl:active:{uid}",
        )
        b.button(text="⛔ Удалить из команды", callback_data=f"pl:block:{uid}")
    elif user.status == UserStatus.BLOCKED:
        b.button(text="♻️ Вернуть в команду", callback_data=f"pl:approve:{uid}")
    b.button(text="← Все игроки", callback_data="pl:list:0")
    b.adjust(1)
    return "\n".join(lines), b.as_markup()


async def players_list(session: AsyncSession) -> tuple[str, InlineKeyboardMarkup | None]:
    users = await svc.players_for_admin(session)
    if not users:
        return "Пока никто не заходил в бота. Отправьте команде ссылку на бота.", None
    pending = sum(u.status == UserStatus.PENDING for u in users)
    lines = ["<b>🗂 Игроки</b>", ""]
    if pending:
        lines.append(f"⏳ Ждут подтверждения: {pending} — нажмите, чтобы принять или отклонить.")
    lines += [
        "✅ в команде · 🚫 временно не в составе · ⛔ заблокирован · 👑 админ · 🚗 есть машина",
        "",
        "Нажмите на игрока, чтобы изменить машину, имя или статус.",
    ]
    b = InlineKeyboardBuilder()
    for u in users:
        icon = "🚫" if u.status == UserStatus.APPROVED and not u.is_active else STATUS_ICONS.get(u.status, "")
        crown = " 👑" if config.is_owner(u.telegram_id) or u.is_admin else ""
        b.button(text=f"{icon} {u.name}{crown}{' 🚗' if u.has_car else ''}", callback_data=f"pl:show:{u.id}")
    b.adjust(2)
    return "\n".join(lines), b.as_markup()
