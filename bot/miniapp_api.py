"""API для Telegram Mini App: вход по подписи Telegram (initData), все действия — через operations."""

import hashlib
import hmac
import json
import time
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from urllib.parse import parse_qsl

from aiogram import Bot
from sqlalchemy.ext.asyncio import AsyncSession

from bot import actions, operations, texts, whatsapp
from bot.config import config
from bot.deadlines import penalties_enabled, rsvp_deadline
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


# ----------------------------------------------------------------- представления


def _user_brief(u: User) -> dict:
    return {"id": u.id, "name": u.name, "car": u.has_car}


async def game_view(session: AsyncSession, game: Game, me: User, is_admin: bool) -> dict:
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
        "date_iso": game.starts_at.date().isoformat(),
        "minutes": game.starts_at.hour * 60 + game.starts_at.minute,
        "location": game.location,
        "status": game.status,
        "my_rsvp": await svc.get_rsvp(session, game.id, me.id),
        "deadline_label": texts.until(deadline, now),
        "deadline_at_label": f"{texts.day_word(deadline, now).lower()} в {texts.fmt_time(deadline)}",
        "deadline_passed": now >= deadline,
        "penalty": config.penalty_points if penalties_enabled(game) and not game.penalties_applied else 0,
        "participants": {s: [_user_brief(u) for u in by_status[s]] for s in Rsvp.ALL},
        "min_players": game.min_players or 0,
        "needs_decision": is_admin and operations.needs_min_decision(game, len(by_status[Rsvp.YES])),
        "waiting_until": texts.fmt_time(game.min_recheck_at) if game.min_recheck_at else None,
        "from_schedule": game.schedule_id is not None,
        "duties": duties,
    }
    if is_admin:
        view["no_answer"] = [_user_brief(u) for u in await svc.non_responders(session, game)]
    return view


async def state_view(bot: Bot, session: AsyncSession, tg: dict, user: User | None) -> dict:
    is_admin = config.is_admin(int(tg["id"]))
    me = await bot.me()
    data: dict[str, Any] = {
        "bot_username": me.username,
        "is_admin": is_admin,
        "is_owner": config.is_owner(int(tg["id"])),
        "tg_name": await svc.name_from_telegram(session, _tg_ns(tg)),
        "user": None,
        "games": [],
        "penalty_points": config.penalty_points,
    }
    if user is None or not user.profile_completed:
        data["access"] = "new"
        return data
    data["user"] = {
        "id": user.id, "name": user.name, "car": user.has_car, "car_locked": user.car_locked,
        "status": user.status, "active": user.is_active,
        "minuses": (await svc.open_penalty_points(session, [user.id])).get(user.id, 0),
    }
    data["access"] = "ok" if (user.is_approved or is_admin) else user.status
    if data["access"] == "ok":
        games = [g for g in await svc.upcoming_games(session, config.now()) if g.status in GameStatus.ACTIVE]
        data["games"] = [await game_view(session, g, user, is_admin) for g in games]
        if is_admin:
            players = await svc.players_for_admin(session)
            data["pending_count"] = sum(p.status == UserStatus.PENDING for p in players)
            data["schedules"] = [schedule_view(x) for x in await svc.schedules(session)]
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
        "location": x.location, "min_players": x.min_players or 0, "minutes": x.minutes, "open_days_before": x.open_days_before,
        "active": x.is_active, "next_label": texts.fmt_date(nxt, weekday=True),
    }


def _tg_ns(tg: dict) -> SimpleNamespace:
    return SimpleNamespace(id=int(tg["id"]), first_name=tg.get("first_name", ""), last_name=tg.get("last_name", ""))


async def stats_view(session: AsyncSession) -> dict:
    minuses = await svc.open_penalty_points(session)
    return {
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
            {"id": p.id, "points": p.points, "game": texts.game_header(p.game) if p.game else ""} for p in penalties
            if p.status == PenaltyStatus.OPEN
        ],
    }
    if is_admin:
        view.update(
            username=user.username, status=user.status, active=user.is_active, car_locked=user.car_locked,
            telegram_id=user.telegram_id, is_admin=bool(user.is_admin), is_owner=config.is_owner(user.telegram_id),
        )
    return view


async def players_view(session: AsyncSession) -> dict:
    minuses = await svc.open_penalty_points(session)
    return {
        "players": [
            {
                "id": u.id, "name": u.name, "username": u.username, "car": u.has_car, "car_locked": u.car_locked,
                "status": u.status, "active": u.is_active, "minuses": minuses.get(u.id, 0),
                "admin": bool(u.is_admin) or config.is_owner(u.telegram_id),
            }
            for u in await svc.players_for_admin(session)
        ]
    }


# ----------------------------------------------------------------- действия


class ApiError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


PUBLIC_ACTIONS = {"state", "register"}
ADMIN_ACTIONS = {
    "duties", "duty_update", "min_decide", "update_game", "create_game", "schedule_update", "schedule_delete", "distribute", "cancel", "assign", "players", "player", "player_detail", "whatsapp",
    "penalty_cancel",
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
    action = body.get("action", "state")
    tg_id = int(tg["id"])
    await svc.refresh_admins(session)
    is_admin = config.is_admin(tg_id)
    user = await svc.get_user_by_tg(session, tg_id)
    if user is not None:
        user.username = tg.get("username") or user.username
        if is_admin and user.status != UserStatus.APPROVED:
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
            game = await _game(session, body)
            result = await operations.change_rsvp(bot, session, game, user, body.get("status", ""))
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
        elif action == "stats":
            return await stats_view(session)
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
                    open_days_before=max(1, min(6, int(body.get("open_days_before") or 2))),
                )
                session.add(schedule)
                await session.flush()
            game, poll_report, hint, announce = await operations.create_game(
                bot, session, user, body.get("kind", "game"), starts_at, body.get("location"),
                min_players, schedule.id if schedule else None,
            )
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
                int(body.get("min_players") or 0),
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
            if "kind" in body and body["kind"] in texts.KIND_TITLES:
                x.kind = body["kind"]
            if {"weekday", "minutes", "location", "kind"} & body.keys():
                note = f"Расписание: {EVERY_WEEKDAY[x.weekday]} в {x.time_label}. Уже созданные игры не меняются."
        elif action == "schedule_delete":
            x = await session.get(Schedule, int(body.get("id", 0)))
            if x is not None:
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
            return {"note": "Минус снят", "player": await player_view(session, await _user(session, penalty.user_id), True)}
        else:
            raise ApiError("Неизвестное действие.", 404)
    except operations.OpError as e:
        raise ApiError(str(e)) from e
    except (ValueError, KeyError, TypeError) as e:
        raise ApiError("Неверные данные запроса.") from e

    await session.flush()
    return {"note": note, "state": await state_view(bot, session, tg, user)}
