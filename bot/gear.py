"""Мячи и манишки между играми.

После игры обязанности «после» (⚽ Мячи, 👕 Манишки) — «забрать и привезти на следующую». Значит, к следующей
игре вещи у того, кто их забрал. Если он ставит «Не буду» / «Не знаю» (или молчит до закрытия сбора) — бот
сразу просит передать их заранее тому, кто придёт, и предупреждает админов. Передал — вещи теперь «у него»;
если и он не придёт, цепочка повторяется.
"""

from dataclasses import dataclass
from datetime import timedelta

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot import notifier, texts
from bot.models import Assignment, AssignmentStatus, Duty, Game, GameStatus, GearHandover, Rsvp, User
from bot.services import games as svc

GEAR_CODES = ("balls", "bibs")
STALE = timedelta(days=45)  # вещи «с прошлой игры» старше полутора месяцев — уже не знаем, у кого
WHAT = {"balls": "мячи", "bibs": "манишки"}


@dataclass
class Holder:
    duty: Duty
    user: User  # у кого вещи сейчас
    source: Game  # с какой игры забрал
    coming: bool  # отметил «Буду» на эту игру
    handed_from: User | None = None  # передал ему другой (тот не придёт)
    self_arranged: bool = False  # «договорюсь сам» — передаст без бота

    @property
    def what(self) -> str:
        return WHAT.get(self.duty.code or "", self.duty.name.lower())

    @property
    def needs_handover(self) -> bool:
        return not self.coming and not self.self_arranged


def is_gear(duty: Duty) -> bool:
    return duty.code in GEAR_CODES or "следующ" in (duty.action or "")


async def holders(session: AsyncSession, game: Game) -> list[Holder]:
    """У кого мячи/манишки к этой игре (только для игр, где ещё идёт сбор)."""
    if game.status not in GameStatus.ACTIVE:
        return []
    out = []
    for duty in await svc.active_duties(session):
        if not is_gear(duty):
            continue
        row = (await session.execute(
            select(Assignment, Game).join(Game, Game.id == Assignment.game_id)
            .where(Assignment.duty_id == duty.id, Assignment.status == AssignmentStatus.ACTIVE,
                   Game.starts_at < game.starts_at, Game.starts_at >= game.starts_at - STALE,
                   Game.status.not_in(GameStatus.NOT_PLAYED))
            .order_by(Game.starts_at.desc()).limit(1)
        )).first()
        if row is None:
            continue
        assignment, source = row
        between = await session.scalar(select(Game.id).where(  # до этой игры будет ещё одна — вещи поедут туда
            Game.starts_at > source.starts_at, Game.starts_at < game.starts_at,
            Game.status.not_in((*GameStatus.NOT_PLAYED, GameStatus.PLANNED))).limit(1))
        if between is not None:
            continue
        user, handed_from, self_arranged = assignment.user, None, False
        for h in (await session.scalars(select(GearHandover).where(
                GearHandover.game_id == game.id, GearHandover.duty_id == duty.id).order_by(GearHandover.id))).all():
            if h.to_user_id:
                handed_from, user, self_arranged = await session.get(User, h.from_user_id), await session.get(User, h.to_user_id), False
            elif h.from_user_id == user.id:
                self_arranged = True
        if user is None:
            continue
        coming = await svc.get_rsvp(session, game.id, user.id) == Rsvp.YES
        out.append(Holder(duty, user, source, coming, handed_from, self_arranged))
    return out


async def coming_players(session: AsyncSession, game: Game, exclude: int) -> list[User]:
    users = (await svc.participants_by_status(session, game.id))[Rsvp.YES]
    return sorted((u for u in users if u.id != exclude and not u.is_staff), key=lambda u: u.name.lower())


def picker(game: Game, duty: Duty, users: list[User]) -> InlineKeyboardMarkup:
    rows, row = [], []
    for u in users:
        row.append(InlineKeyboardButton(text=u.name, callback_data=f"gh:{game.id}:{duty.id}:{u.id}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton(text="🤝 Договорюсь сам", callback_data=f"gh:{game.id}:{duty.id}:0")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def ask_text(game: Game, h: Holder) -> str:
    return (f"🎒 <b>{texts.game_header(game)}</b>\n"
            f"{h.duty.emoji} {h.what.capitalize()} у тебя (забрал после: {texts.game_header(h.source)}), "
            f"а ты не идёшь. Передай их заранее тому, кто будет, — иначе команда останется без них.\n"
            "Кому передашь?")


async def warn(bot: Bot, session: AsyncSession, game: Game, user: User, to_admins: bool = True) -> list[str]:
    """Держатель вещей не идёт: ему — «кому передашь?», админам — строка. Возвращает строки для админов."""
    lines = []
    for h in await holders(session, game):
        if h.user.id != user.id or not h.needs_handover:
            continue
        users = await coming_players(session, game, user.id)
        await notifier.send_dm(bot, user, ask_text(game, h) + ("" if users else "\nПока никто не отметил «Буду» — напиши в чат команды."),
                               picker(game, h.duty, users))
        lines.append(f"🎒 {h.duty.emoji} {h.what.capitalize()} держит {texts.h(user.name)} — не идёт на игру, бот попросил передать заранее")
    if lines and to_admins:
        await notifier.notify_admins(bot, f"{texts.game_header(game)}\n" + "\n".join(lines))
    return lines


async def handover(bot: Bot, session: AsyncSession, game: Game, duty: Duty, actor: User, to_user: User | None,
                   by_admin: bool = False) -> str:
    h = next((x for x in await holders(session, game) if x.duty.id == duty.id), None)
    if h is None:
        raise ValueError("Не знаю, у кого сейчас эти вещи.")
    if h.user.id != actor.id and not by_admin:
        raise ValueError(f"{h.what.capitalize()} сейчас держит {h.user.name} — передать может только тот, у кого вещи, или админ.")
    if to_user is not None and to_user.id == h.user.id:
        raise ValueError(f"{h.what.capitalize()} и так держит {to_user.name}.")
    holder = h.user
    session.add(GearHandover(game_id=game.id, duty_id=duty.id, from_user_id=holder.id,
                             to_user_id=to_user.id if to_user else None))
    await session.flush()
    header = texts.game_header(game)
    if to_user is None:
        await notifier.notify_admins(bot, f"🎒 {texts.h(holder.name)}: {duty.emoji} {h.what} передаст сам, без бота ({header})")
        return f"Ок, договоришься сам. Главное — чтобы {h.what} были на игре."
    await notifier.send_dm(bot, to_user, f"🎒 <b>{header}</b>\n{texts.h(holder.name)} не идёт и передаст тебе "
                                         f"{duty.emoji} {h.what} — договоритесь, где забрать. Привези их на игру 🙏")
    await notifier.notify_admins(bot, f"🎒 {duty.emoji} {h.what}: {texts.h(holder.name)} → {texts.h(to_user.name)} ({header})")
    if by_admin and actor.id != holder.id:
        await notifier.send_dm(bot, holder, f"🎒 Админ: {duty.emoji} {h.what} передай {texts.h(to_user.name)} — "
                                            f"он будет на игре ({header}).")
    return f"✅ {h.what.capitalize()} передаёшь {to_user.name} — ему пришло сообщение."


async def lines_for_card(session: AsyncSession, game: Game, me: User | None) -> list[str]:
    """Строки для карточки игры в боте."""
    out = []
    for h in await holders(session, game):
        who = "у тебя" if me and h.user.id == me.id else f"держит {texts.h(h.user.name)}"
        line = f"🎒 {h.duty.emoji} {h.what.capitalize()} {who}"
        if h.handed_from:
            line += f" (передал {texts.h(h.handed_from.name)})"
        if h.needs_handover:
            line += " — ⚠️ не идёт, нужно передать"
        elif h.self_arranged and not h.coming:
            line += " — передаст сам"
        out.append(line)
    return out
