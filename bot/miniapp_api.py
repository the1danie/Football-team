"""API для Telegram Mini App: вход по подписи Telegram (initData), все действия — через operations."""

import hashlib
import hmac
import json
import re
import time
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any
from urllib.parse import parse_qsl

from aiogram import Bot
from sqlalchemy.ext.asyncio import AsyncSession

from bot import actions, audit, operations, texts, whatsapp
from bot.config import config
from bot.deadlines import penalties_enabled, rsvp_deadline, to_utc
from bot.models import EVERY_WEEKDAY, WEEKDAYS_FULL, Duty, DutyPhase, Game, GameStatus, PenaltyStatus, Rsvp, Schedule, User, UserStatus
from bot.services import games as svc

INIT_DATA_MAX_AGE = 24 * 3600


# ----------------------------------------------------------------- проверка подписи


def verify_init_data(init_data: str, bot_token: str, max_age: int = INIT_DATA_MAX_AGE) -> dict | None:
    """Проверить initData от Telegram. Возвращает данные пользователя или None.

    https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app
    """
    if not init_data or not bot_token:
        return None
    fields = dict(parse_qsl(init_data, keep_blank_values=True))
    received = fields.pop("hash", "")
    check_string = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, received):
        return None
    try:
        if time.time() - int(fields.get("auth_date", "0")) > max_age:
            return None
        user = json.loads(fields.get("user", "{}"))
    except ValueError:
        return None
    return user if user.get("id") else None


def sign_init_data(user: dict, bot_token: str, auth_date: int | None = None) -> str:
    """Подписать initData (для тестов и локальной разработки)."""
    from urllib.parse import urlencode

    fields = {"auth_date": str(auth_date or int(time.time())), "user": json.dumps(user, ensure_ascii=False)}
    check_string = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    return urlencode(fields)


from bot.weblink import WEB_TOKEN_DAYS, make_web_token, verify_web_token, web_link  # noqa: E402, F401


# ----------------------------------------------------------------- представления


# Через сколько после начала игра уходит из «Игр» в «Прошедшие».
PAST_AFTER_HOURS = 2

# Главный админ может посмотреть приложение глазами другой роли (данные — настоящие, меняются только права).
VIEW_AS = {
    "admin": (True, False, None),  # выданный админ: без выдачи прав
    "player": (False, False, None),
    "staff": (False, False, "Тренер"),
}


def perms(tg: dict, user: User | None) -> tuple[bool, bool, str | None]:
    """(админ, главный админ, роль в штабе) — с учётом режима «посмотреть как»."""
    tg_id = int(tg["id"])
    if config.is_owner(tg_id) and tg.get("view_as") in VIEW_AS:
        return VIEW_AS[tg["view_as"]]
    return config.is_admin(tg_id), config.is_owner(tg_id), user.staff_title if user else None


def _user_brief(u: User) -> dict:
    return {"id": u.id, "name": u.name, "car": u.has_car, "manual": u.is_manual}


async def game_view(session: AsyncSession, game: Game, me: User, is_admin: bool, staff: bool = False) -> dict:
    now = config.now()
    by_status = await svc.participants_by_status(session, game.id)
    deadline = rsvp_deadline(game)
    assignments = await svc.active_assignments(session, game.id) if game.status == GameStatus.DISTRIBUTED else []
    unassigned = await svc.unassigned_duties(session, game) if game.status == GameStatus.DISTRIBUTED else []
    duties = [
        {
            "duty_id": a.duty.id, "emoji": a.duty.emoji, "name": a.duty.name, "action": a.duty.action,
            "requires_car": a.duty.requires_car, "user": _user_brief(a.user),
            "assignment_id": a.id, "mine": a.user_id == me.id,
        }
        for a in assignments
    ] + [
        {
            "duty_id": d.id, "emoji": d.emoji, "name": d.name, "action": d.action,
            "requires_car": d.requires_car, "user": None, "assignment_id": None, "mine": False,
        }
        for d in unassigned
    ]
    from bot.actions import after_at

    marks = await svc.attendance(session, game.id)
    before = await svc.active_duties(session, DutyPhase.BEFORE)
    pending_after = await svc.pending_after_duties(session, game)
    view = {
        "before_titles": [d.title for d in before],
        "after_pending": [d.title for d in pending_after],
        "after_time": texts.fmt_time(after_at(game)),
        "id": game.id,
        "kind": game.kind,
        "kind_title": texts.kind_title(game),
        "date_label": texts.fmt_date(game.starts_at, weekday=True),
        "day_word": texts.day_word(game.starts_at, now),
        "time": texts.fmt_time(game.starts_at),
        "gather_time": texts.gather_time(game),
        "date_iso": game.starts_at.date().isoformat(),
        "minutes": game.starts_at.hour * 60 + game.starts_at.minute,
        "location": game.location,
        "location_url": game.location_url,
        "map_url": texts.map_url(game.location, game.location_url),
        "map_label": texts.map_label(texts.map_url(game.location, game.location_url)),
        "status": game.status,
        "past": game.status not in GameStatus.ACTIVE or now >= game.starts_at + timedelta(hours=PAST_AFTER_HOURS),
        "my_rsvp": await svc.get_rsvp(session, game.id, me.id),
        "deadline_label": texts.until(deadline, now),
        "deadline_at_label": f"{texts.day_word(deadline, now).lower()} в {texts.fmt_time(deadline)}",
        "deadline_passed": now >= deadline,
        # Для живого таймера: момент закрытия сбора в мс (UTC) и «сейчас» сервера — на случай неверных часов телефона.
        "deadline_ts": int(to_utc(deadline).replace(tzinfo=timezone.utc).timestamp() * 1000),
        "server_ts": int(datetime.now(timezone.utc).timestamp() * 1000),
        "silence_penalized": bool(game.penalties_applied) and config.penalty_points > 0,
        "penalty": config.penalty_points if penalties_enabled(game) and not game.penalties_applied else 0,
        "participants": {
            s: [{**_user_brief(u), "attended": marks.get(u.id)} for u in by_status[s]] for s in Rsvp.ALL
        },
        "attendance_open": game.status != GameStatus.CANCELLED and now >= game.starts_at - timedelta(hours=1),
        "min_players": game.min_players or 0,
        "needs_decision": is_admin and operations.needs_min_decision(game, len(by_status[Rsvp.YES])),
        "waiting_until": texts.fmt_time(game.min_recheck_at) if game.min_recheck_at else None,
        "from_schedule": game.schedule_id is not None,
        "duties": duties,
    }
    if is_admin or staff:
        view["no_answer"] = [_user_brief(u) for u in await svc.non_responders(session, game)]
        view["team_count"] = len(await svc.roster(session))
        if (
            is_admin and now >= deadline and game.penalties_applied and penalties_enabled(game)
            and config.penalty_points > 0
        ):
            view["silent_unpenalized"] = [_user_brief(u) for u in await svc.unpenalized_silent(session, game)]
        if not penalties_enabled(game):
            view["no_penalty_reason"] = "Минусов за молчание не будет: игру создали меньше чем за час до конца сбора."
        elif config.penalty_points <= 0:
            view["no_penalty_reason"] = "Минусы за молчание выключены (PENALTY_POINTS=0)."
    return view


async def state_view(bot: Bot, session: AsyncSession, tg: dict, user: User | None) -> dict:
    is_admin, is_owner, staff = perms(tg, user)
    me = await bot.me()
    data: dict[str, Any] = {
        "bot_username": me.username,
        "is_admin": is_admin,
        "is_owner": is_owner,
        "real_owner": config.is_owner(int(tg["id"])),
        "view_as": tg.get("view_as") if config.is_owner(int(tg["id"])) and tg.get("view_as") in VIEW_AS else None,
        "tg_name": await svc.name_from_telegram(session, _tg_ns(tg)),
        "user": None,
        "games": [],
        "penalty_points": config.penalty_points,
        "after_minutes": int(config.after_duties_minutes),
        "web": "web_version" in tg,
    }
    if user is None or not user.profile_completed:
        data["access"] = "new"
        return data
    data["user"] = {
        "id": user.id, "name": user.name, "car": user.has_car, "car_locked": user.car_locked,
        "status": user.status, "active": user.is_active, "staff": staff,
        "minuses": (await svc.open_penalty_points(session, [user.id])).get(user.id, 0),
    }
    data["access"] = "ok" if (user.is_approved or is_admin) else user.status
    if data["access"] == "ok":
        games = [
            g for g in await svc.upcoming_games(session, config.now(), grace_hours=PAST_AFTER_HOURS)
            if g.status in GameStatus.ACTIVE
        ]
        data["games"] = [await game_view(session, g, user, is_admin, bool(staff)) for g in games]
        if is_admin:
            players = await svc.players_for_admin(session)
            data["pending_count"] = sum(p.status == UserStatus.PENDING for p in players)
            data["schedules"] = [schedule_view(x) for x in await svc.schedules(session)]
            data["places"] = await svc.places(session)
    return data


def duty_view(d: Duty) -> dict:
    return {
        "id": d.id, "emoji": d.emoji, "name": d.name, "action": d.action, "phase": d.phase,
        "requires_car": d.requires_car, "active": d.is_active,
    }


def schedule_view(x: Schedule) -> dict:
    nxt = svc.next_occurrence(x, config.now())
    return {
        "id": x.id, "kind": x.kind, "kind_title": texts.KIND_TITLES.get(x.kind, ""),
        "weekday": x.weekday, "weekday_label": WEEKDAYS_FULL[x.weekday], "every_label": EVERY_WEEKDAY[x.weekday],
        "time": x.time_label,
        "location": x.location, "location_url": x.location_url, "min_players": x.min_players or 0, "minutes": x.minutes, "open_days_before": x.open_days_before,
        "active": x.is_active, "next_label": texts.fmt_date(nxt, weekday=True),
    }


def _tg_ns(tg: dict) -> SimpleNamespace:
    return SimpleNamespace(id=int(tg["id"]), first_name=tg.get("first_name", ""), last_name=tg.get("last_name", ""))


async def stats_view(session: AsyncSession, period: str = "all") -> dict:
    minuses = await svc.open_penalty_points(session)
    now = config.now()
    since = now - timedelta(days=30) if period == "month" else None
    return {
        "period": "month" if since else "all",
        "board": await svc.leaderboard(session, now, since),
        "team": [
            {"id": u.id, "name": u.name, "total": n, "minuses": minuses.get(u.id, 0), "car": u.has_car}
            for u, n in await svc.team_stats(session)
        ],
    }


async def player_view(session: AsyncSession, user: User, is_admin: bool) -> dict:
    penalties = await svc.user_penalties(session, user.id, open_only=True)
    view = {
        "id": user.id, "name": user.name, "car": user.has_car,
        "duties": [{"emoji": d.emoji, "name": d.name, "count": n} for d, n in await svc.player_stats(session, user.id)],
        "minuses": [
            {"id": p.id, "points": p.points, "game": texts.game_header(p.game) if p.game else "",
             "reason": texts.penalty_reason(p.reason)} for p in penalties
            if p.status == PenaltyStatus.OPEN
        ],
    }
    if is_admin:
        view.update(
            username=user.username, status=user.status, active=user.is_active, car_locked=user.car_locked,
            staff=user.staff_title, manual=user.is_manual,
            telegram_id=user.telegram_id, is_admin=bool(user.is_admin), is_owner=config.is_owner(user.telegram_id),
        )
    return view


async def players_view(session: AsyncSession) -> dict:
    minuses = await svc.open_penalty_points(session)
    return {
        "players": [
            {
                "id": u.id, "name": u.name, "username": u.username, "car": u.has_car, "car_locked": u.car_locked,
                "status": u.status, "active": u.is_active, "minuses": minuses.get(u.id, 0), "staff": u.staff_title,
                "manual": u.is_manual,
                "admin": bool(u.is_admin) or config.is_owner(u.telegram_id),
            }
            for u in await svc.players_for_admin(session)
        ]
    }


# ----------------------------------------------------------------- вход по имени и PIN


async def pin_login(session: AsyncSession, name: str, pin: str) -> str:
    """Вход на сайт без Telegram и без ссылки: имя + PIN от админа. Возвращает личный ключ."""
    from bot.weblink import PIN_LOCK_MINUTES, PIN_MAX_FAILS, pin_hash

    name, pin = (name or "").strip().casefold(), (pin or "").strip()
    wrong = ApiError("Имя или PIN не подходят. PIN выдаёт админ команды.", 403)
    if not name or not pin.isdigit():
        raise wrong
    matches = [u for u in await svc.all_users(session)
               if u.pin_hash and u.status == UserStatus.APPROVED and u.name.strip().casefold() == name]
    if len(matches) != 1:
        raise wrong
    user = matches[0]
    now = datetime.utcnow()
    if user.pin_locked_until and user.pin_locked_until > now:
        raise ApiError(f"Слишком много попыток. Попробуйте через {PIN_LOCK_MINUTES} минут или попросите у админа новый PIN.", 429)
    if not hmac.compare_digest(user.pin_hash, pin_hash(user.id, pin)):
        user.pin_fails = (user.pin_fails or 0) + 1
        if user.pin_fails >= PIN_MAX_FAILS:
            user.pin_fails, user.pin_locked_until = 0, now + timedelta(minutes=PIN_LOCK_MINUTES)
        await session.commit()  # счётчик попыток сохраняем, даже когда отвечаем ошибкой
        raise wrong
    user.pin_fails, user.pin_locked_until = 0, None
    return make_web_token(user.telegram_id, user.web_version or 0, config.bot_token)


# ----------------------------------------------------------------- действия


class ApiError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


PUBLIC_ACTIONS = {"state", "register"}
ADMIN_ACTIONS = {
    "team_invite", "attendance", "duties", "duty_update", "min_decide", "update_game", "create_game", "schedule_update", "schedule_delete", "distribute", "cancel", "assign", "players", "player", "player_detail", "whatsapp",
    "penalty_cancel", "penalize_silent", "player_add", "player_invite", "rsvp_for", "delete_game", "player_web_link", "player_pin",
}


async def _game(session: AsyncSession, body: dict) -> Game:
    game = await svc.get_game(session, int(body.get("game_id", 0)))
    if game is None:
        raise ApiError("Игра не найдена.", 404)
    return game


async def _user(session: AsyncSession, raw_id: Any) -> User:
    user = await session.get(User, int(raw_id or 0))
    if user is None:
        raise ApiError("Игрок не найден.", 404)
    return user


async def handle(bot: Bot, session: AsyncSession, tg: dict, body: dict) -> dict:
    """Выполнить действие из Mini App. Возвращает данные для ответа."""
    me = await svc.get_user_by_tg(session, int(tg["id"]))
    with audit.acting(int(tg["id"]), me.id if me else None, me.name if me else tg.get("first_name", "")):
        return await _handle(bot, session, tg, body)


async def _handle(bot: Bot, session: AsyncSession, tg: dict, body: dict) -> dict:
    action = body.get("action", "state")
    tg_id = int(tg["id"])
    await svc.refresh_admins(session)
    user = await svc.get_user_by_tg(session, tg_id)
    if body.get("view_as") in VIEW_AS and config.is_owner(tg_id):
        tg = {**tg, "view_as": body["view_as"]}
    is_admin, _, staff_view = perms(tg, user)
    if "web_version" in tg:  # вход по личной ссылке из браузера
        if user is None or (user.web_version or 0) != tg["web_version"]:
            raise ApiError("Ссылка устарела. Попросите новую в боте командой /web.", 401)
        if action == "register":
            raise ApiError("Регистрация — через бота в Telegram (/start).", 403)
    if user is not None:
        user.username = tg.get("username") or user.username
        if config.is_admin(tg_id) and user.status != UserStatus.APPROVED:
            user.status = UserStatus.APPROVED

    if action not in PUBLIC_ACTIONS:
        if user is None or not user.profile_completed:
            raise ApiError("Сначала заполните профиль.", 403)
        if user.status == UserStatus.BLOCKED and not is_admin:
            raise ApiError("Доступ закрыт администратором.", 403)
        if not user.is_approved and not is_admin:
            raise ApiError("Заявка у администратора — дождитесь подтверждения.", 403)
    if action in ADMIN_ACTIONS and not is_admin:
        raise ApiError("Только для администратора.", 403)

    note = None
    try:
        if action == "state":
            pass
        elif action == "register":
            if user is not None and user.status == UserStatus.BLOCKED:
                raise ApiError("Доступ закрыт администратором.", 403)
            name = (body.get("name") or "").strip() or await svc.name_from_telegram(session, _tg_ns(tg))
            user = await operations.complete_registration(
                bot, session, tg_id, name[:64], tg.get("username"), bool(body.get("car"))
            )
        elif action == "rsvp":
            if staff_view and not user.is_staff:
                raise ApiError("Тренер не отмечается — это просмотр глазами штаба.")
            game = await _game(session, body)
            try:
                result = await operations.change_rsvp(bot, session, game, user, body.get("status", ""))
            except operations.RsvpLocked:
                note = await operations.request_rsvp_change(bot, session, game, user, body.get("status", ""))
                return {"note": note, "state": await state_view(bot, session, tg, user)}
            if result.reassigned:
                note = "Обязанность передана другому игроку."
        elif action == "profile":
            if "name" in body:
                await operations.rename_self(session, user, body["name"])
                note = "Имя сохранено"
            if "car" in body:
                await operations.set_own_car(bot, session, user, bool(body["car"]))
                note = "Сохранено"
        elif action == "swap_targets":
            a = await session.get(svc.Assignment, int(body.get("assignment_id", 0)))
            if a is None or a.user_id != user.id:
                raise ApiError("Назначение уже неактуально.")
            game = await svc.get_game(session, a.game_id)
            return {
                "targets": [
                    {"id": u.id, "name": u.name, "duty": {"emoji": t.duty.emoji, "name": t.duty.name} if t else None}
                    for u, t in await svc.swap_targets(session, game, a)
                ]
            }
        elif action == "swap":
            target = await operations.offer_swap(
                bot, session, user, int(body.get("assignment_id", 0)), int(body.get("user_id", 0))
            )
            note = f"Предложение отправлено: {target.name}. Ответ придёт в чат с ботом."
        elif action == "web_link":
            link = web_link(user)
            if link is None:
                raise ApiError("Веб-версия доступна, когда бот работает на Vercel.")
            return {"url": link}
        elif action == "web_logout":
            user.web_version = (user.web_version or 0) + 1
            note = "Все личные ссылки отключены. Новую можно получить в боте: /web."
        elif action == "audit":
            if not config.is_owner(tg_id) or tg.get("view_as"):
                raise ApiError("Журнал видит только главный админ.", 403)
            offset = max(0, int(body.get("offset") or 0))
            actor = int(body.get("actor_id") or 0) or None
            rows = await svc.audit_entries(session, actor, limit=51, offset=offset)
            now = config.now()

            def when(dt):
                local = dt.replace(tzinfo=timezone.utc).astimezone(config.tz).replace(tzinfo=None)
                delta = (now.date() - local.date()).days
                day = "Сегодня" if delta == 0 else "Вчера" if delta == 1 else texts.fmt_date(local, weekday=True)
                return {"day": day, "time": texts.fmt_time(local)}

            return {
                "entries": [{"id": r.id, "actor": r.actor_name, "actor_id": r.actor_id, "text": r.text, **when(r.created_at)}
                            for r in rows[:50]],
                "more": len(rows) > 50,
                "actors": [{"id": a, "name": n, "count": c} for a, n, c in await svc.audit_actors(session)],
            }
        elif action == "archive":
            offset = max(0, int(body.get("offset") or 0))
            past = await svc.past_games(session, config.now(), limit=11, offset=offset, past_after_hours=PAST_AFTER_HOURS)
            return {
                "archive": [await game_view(session, g, user, is_admin, bool(staff_view)) for g in past[:10]],
                "more": len(past) > 10,
            }
        elif action == "attendance_report":
            if not (is_admin or staff_view):
                raise ApiError("Только для админа и тренера.", 403)
            since = config.now() - timedelta(days=30) if body.get("period") == "month" else None
            return {"rows": await svc.attendance_report(session, config.now(), since),
                    "period": "month" if since else "all"}
        elif action == "stats":
            return await stats_view(session, body.get("period", "all"))
        elif action == "player_stats":
            return await player_view(session, await _user(session, body.get("user_id")), is_admin)
        # --- админ
        elif action == "create_game":
            day = date.fromisoformat(body.get("date", ""))
            minutes = int(body.get("minutes", -1))
            if not 0 <= minutes <= 24 * 60:
                raise ApiError("Выберите время.")
            starts_at = datetime.combine(day, datetime.min.time()) + timedelta(minutes=minutes)
            min_players = int(body.get("min_players") or 0)
            schedule = None
            if body.get("repeat"):
                # Каждую неделю в этот день и время; первая игра — эта.
                schedule = Schedule(
                    kind=body.get("kind", "training"), weekday=day.weekday(), minutes=minutes,
                    location=(body.get("location") or "").strip()[:255] or None, min_players=min_players or None,
                    location_url=texts.normalize_map_url(body.get("location_url")),
                    open_days_before=max(1, min(6, int(body.get("open_days_before") or 2))),
                )
                session.add(schedule)
                await session.flush()
                await audit.record(session, f"🔁 Создал расписание {EVERY_WEEKDAY[schedule.weekday]} в {schedule.time_label}")
                # Опрос — только когда откроется окно (за open_days_before дней), а не сразу.
                first = svc.next_occurrence(schedule, config.now())
                opens_at = first - timedelta(days=schedule.open_days_before)
                if config.now() < opens_at:
                    schedule.location_url = schedule.location_url or await svc.known_place_url(session, schedule.location)
                    await session.flush()
                    return {
                        "scheduled": True,
                        "message": (
                            f"🔁 Расписание сохранено: {EVERY_WEEKDAY[schedule.weekday]} в {schedule.time_label}.\n"
                            f"Ближайшая — {texts.fmt_date(first, weekday=True)}. Опрос игрокам уйдёт "
                            f"{texts.fmt_date(opens_at, weekday=True)} в {texts.fmt_time(opens_at)} "
                            f"(за {schedule.open_days_before} {texts.days_word(schedule.open_days_before)}) — "
                            "тогда же придёт анонс для WhatsApp."
                        ),
                        "state": await state_view(bot, session, tg, user),
                    }
            game, poll_report, hint, announce = await operations.create_game(
                bot, session, user, body.get("kind", "game"), starts_at, body.get("location"),
                min_players, schedule.id if schedule else None, body.get("location_url"),
            )
            if schedule and not schedule.location_url:
                schedule.location_url = game.location_url
            if schedule:
                hint += (
                    f" 🔁 Дальше — {EVERY_WEEKDAY[schedule.weekday]} в {schedule.time_label}, "
                    f"опрос за {schedule.open_days_before} дн."
                )
            await session.flush()
            return {
                "game_id": game.id, "note": f"{poll_report}\n{hint}",
                "whatsapp": {"text": announce, "url": whatsapp.share_url(announce)},
                "state": await state_view(bot, session, tg, user),
            }
        elif action == "delete_game":
            game = await _game(session, body)
            if game.status in GameStatus.ACTIVE and config.now() < game.starts_at + timedelta(hours=PAST_AFTER_HOURS):
                raise ApiError("Удалить можно отменённую или прошедшую игру. Предстоящую сначала отмените.")
            await audit.record(session, "🗑 Удалил из архива", game)
            await svc.delete_game(session, game)
            note = "🗑 Игра удалена"
        elif action == "penalize_silent":
            game = await _game(session, body)
            if not game.penalties_applied or not penalties_enabled(game) or config.penalty_points <= 0:
                raise ApiError("Минусы ставятся после закрытия сбора.")
            from bot.actions import penalize_late_silent

            report = await penalize_late_silent(bot, session, game)
            if report:
                await audit.record(session, "⚠️ " + re.sub(r"<[^>]+>", "", report), game)
            note = "Минусы поставлены" if report else "Некому ставить минус"
        elif action == "team_invite":
            text = whatsapp.team_invite((await bot.me()).username)
            return {"text": text, "url": whatsapp.share_url(text)}
        elif action == "attendance":
            note = await operations.mark_attendance(
                bot, session, await _game(session, body), await _user(session, body.get("user_id")),
                bool(body.get("present")),
            )
        elif action == "duties":
            return {"duties": [duty_view(d) for d in await svc.all_duties(session)]}
        elif action == "duty_update":
            duty = await session.get(Duty, int(body.get("id", 0)))
            if duty is None:
                raise ApiError("Обязанность не найдена.", 404)
            if body.get("phase") in (DutyPhase.BEFORE, DutyPhase.AFTER):
                duty.phase = body["phase"]
            if "requires_car" in body:
                duty.requires_car = bool(body["requires_car"])
            if "active" in body:
                duty.is_active = bool(body["active"])
            await audit.record(session, f"⚙️ Обязанность {duty.title}: " + ", ".join(
                [("до игры" if duty.phase == DutyPhase.BEFORE else "после игры"),
                 ("нужна машина" if duty.requires_car else "без машины"), ("включена" if duty.is_active else "выключена")]))
            await session.flush()
            return {"duties": [duty_view(d) for d in await svc.all_duties(session)]}
        elif action == "min_decide":
            note = await operations.decide_min(bot, session, await _game(session, body), body.get("choice", ""), tg_id)
        elif action == "update_game":
            game = await _game(session, body)
            day = date.fromisoformat(body.get("date", ""))
            minutes = int(body.get("minutes", -1))
            if not 0 <= minutes <= 24 * 60:
                raise ApiError("Выберите время.")
            starts_at = datetime.combine(day, datetime.min.time()) + timedelta(minutes=minutes)
            changes, wa = await operations.update_game(
                bot, session, game, body.get("kind", game.kind), starts_at, body.get("location"),
                int(body.get("min_players") or 0), body.get("location_url"),
            )
            await session.flush()
            return {
                "note": "Сохранено. Отметившимся отправлено сообщение об изменениях.",
                "whatsapp": {"text": wa, "url": whatsapp.share_url(wa)},
                "state": await state_view(bot, session, tg, user),
            }
        elif action == "schedule_update":
            x = await session.get(Schedule, int(body.get("id", 0)))
            if x is None:
                raise ApiError("Расписание не найдено.", 404)
            if "active" in body:
                x.is_active = bool(body["active"])
                note = "Расписание включено" if x.is_active else "Расписание на паузе"
            if "min_players" in body:
                x.min_players = max(0, min(100, int(body["min_players"]))) or None
            if "open_days_before" in body:
                x.open_days_before = max(1, min(6, int(body["open_days_before"])))
            if "weekday" in body:
                x.weekday = max(0, min(6, int(body["weekday"])))
            if "minutes" in body:
                if not 0 <= int(body["minutes"]) <= 24 * 60:
                    raise ApiError("Выберите время.")
                x.minutes = int(body["minutes"])
            if "location" in body:
                x.location = (body["location"] or "").strip()[:255] or None
            if "location_url" in body:
                x.location_url = texts.normalize_map_url(body["location_url"])
            if "kind" in body and body["kind"] in texts.KIND_TITLES:
                x.kind = body["kind"]
            if {"weekday", "minutes", "location", "kind"} & body.keys():
                note = f"Расписание: {EVERY_WEEKDAY[x.weekday]} в {x.time_label}. Уже созданные игры не меняются."
            await audit.record(session, f"🔁 Расписание {EVERY_WEEKDAY[x.weekday]} в {x.time_label}: изменено"
                               + (" (включено)" if x.is_active else " (на паузе)"))
        elif action == "schedule_delete":
            x = await session.get(Schedule, int(body.get("id", 0)))
            if x is not None:
                await audit.record(session, f"🔁 Удалил расписание {EVERY_WEEKDAY[x.weekday]} в {x.time_label}")
                await session.delete(x)
                note = "Расписание удалено. Уже созданные игры остались."
        elif action == "distribute":
            report = await operations.distribute(bot, session, await _game(session, body))
            note = report.split("\n")[0]
        elif action == "cancel":
            await operations.cancel_game(bot, session, await _game(session, body), tg_id)
            note = "Игра отменена"
        elif action == "assign":
            game = await _game(session, body)
            duty = await session.get(Duty, int(body.get("duty_id", 0)))
            if duty is None:
                raise ApiError("Обязанность не найдена.", 404)
            target = await _user(session, body["user_id"]) if body.get("user_id") else None
            await operations.assign_duty(bot, session, game, duty, target)
            note = f"{duty.title} — {target.name if target else 'не назначено'}"
        elif action == "whatsapp":
            text = await actions.whatsapp_snapshot(bot, session, await _game(session, body))
            return {"text": text, "url": whatsapp.share_url(text)}
        elif action == "players":
            return await players_view(session)
        elif action == "player_detail":
            return await player_view(session, await _user(session, body.get("user_id")), True)
        elif action == "player_add":
            target = await operations.add_manual_player(session, body.get("name", ""), bool(body.get("car")))
            return {"note": f"✍️ {target.name} добавлен", "player": await player_view(session, target, True),
                    **await players_view(session)}
        elif action == "player_invite":
            target = await _user(session, body.get("user_id"))
            if not target.is_manual:
                raise ApiError("Игрок уже в Telegram.")
            from bot.weblink import link_payload

            tg_link = f"https://t.me/{(await bot.me()).username}?start={link_payload(target)}"
            text = whatsapp.manual_invite(target.name, tg_link, web_link(target))
            return {"tg_link": tg_link, "web": web_link(target), "text": text, "url": whatsapp.share_url(text)}
        elif action == "player_web_link":
            target = await _user(session, body.get("user_id"))
            if target.status != UserStatus.APPROVED:
                raise ApiError("Сначала примите игрока в команду.")
            link = web_link(target)
            if link is None:
                raise ApiError("Сайт доступен, когда бот работает на Vercel.")
            text = whatsapp.site_invite(target.name, link)
            await audit.record(session, f"🌐 Выдал ссылку на сайт: {target.name}")
            return {"text": text, "url": whatsapp.share_url(text)}
        elif action == "player_pin":
            from bot.weblink import new_pin, pin_hash

            target = await _user(session, body.get("user_id"))
            if target.status != UserStatus.APPROVED:
                raise ApiError("Сначала примите игрока в команду.")
            pin = new_pin()
            target.pin_hash, target.pin_fails, target.pin_locked_until = pin_hash(target.id, pin), 0, None
            await audit.record(session, f"🔢 Выдал PIN для входа на сайт: {target.name}")
            text = whatsapp.pin_invite(target.name, pin, config.public_url and f"{config.public_url}/app")
            return {"pin": pin, "text": text, "url": whatsapp.share_url(text)}
        elif action == "my_pin":
            from bot.weblink import new_pin, pin_hash

            pin = new_pin()
            user.pin_hash, user.pin_fails, user.pin_locked_until = pin_hash(user.id, pin), 0, None
            return {"pin": pin, "name": user.name, "site": config.public_url and f"{config.public_url}/app"}
        elif action == "push_key":
            from bot import webpush

            n = await svc.push_count(session, user.id)
            return {"key": await webpush.public_key(session), "subscribed": n}
        elif action == "push_subscribe":
            sub = body.get("subscription") or {}
            keys = sub.get("keys") or {}
            endpoint = str(sub.get("endpoint") or "")
            if not endpoint.startswith("https://") or not keys.get("p256dh") or not keys.get("auth"):
                raise ApiError("Браузер не дал подписку на уведомления.")
            await svc.save_push(session, user, endpoint[:1000], str(keys["p256dh"])[:200], str(keys["auth"])[:100])
            from bot import webpush

            await webpush.send_to_user(session, user, "🔔 Уведомления включены\nСюда придут опросы на игры, напоминания и обязанности.")
            note = "🔔 Уведомления включены"
        elif action == "push_unsubscribe":
            await svc.delete_push(session, user, str(body.get("endpoint") or ""))
            note = "Уведомления выключены"
        elif action == "rsvp_for":
            target = await _user(session, body.get("user_id"))
            game = await _game(session, body)
            await operations.change_rsvp(bot, session, game, target, body.get("status", ""), by_admin=True)
            note = f"{target.name}: {texts.RSVP_LABELS.get(body.get('status'), '')}"
        elif action == "player" and body.get("op") == "link_to":
            pending = await _user(session, body.get("user_id"))
            manual = await _user(session, body.get("target_id"))
            if pending.is_manual:
                raise ApiError("Выберите заявку из Telegram.")
            await operations.link_account(bot, session, manual, pending.telegram_id, pending.username)
            await session.flush()
            return {"note": f"🔗 {manual.name} связан с Telegram", "player": await player_view(session, manual, True),
                    **await players_view(session)}
        elif action == "player":
            target = await _user(session, body.get("user_id"))
            note = await operations.player_action(
                bot, session, target, body.get("op", ""), body.get("name"), actor_tg=tg_id
            )
            await session.flush()
            return {"note": note, "player": await player_view(session, target, True), **await players_view(session)}
        elif action == "penalty_cancel":
            penalty = await svc.cancel_penalty(session, int(body.get("penalty_id", 0)))
            if penalty is None:
                raise ApiError("Минус уже снят или отработан.")
            await audit.record(session, f"♻️ Снял минус: {(await _user(session, penalty.user_id)).name}", penalty.game)
            return {"note": "Минус снят", "player": await player_view(session, await _user(session, penalty.user_id), True)}
        else:
            raise ApiError("Неизвестное действие.", 404)
    except operations.OpError as e:
        raise ApiError(str(e)) from e
    except (ValueError, KeyError, TypeError) as e:
        raise ApiError("Неверные данные запроса.") from e

    await session.flush()
    return {"note": note, "state": await state_view(bot, session, tg, user)}
