"""Повторяющиеся тренировки и отмена, если не набралось минимума."""

from datetime import datetime, timedelta

from bot import webhook
from bot.config import config
from bot.models import Game, GameStatus, Schedule
from bot.scheduler import tick
from bot import operations
from bot.services import games as svc
from tests.conftest import make_user
from tests.test_miniapp import ADMIN, api, team  # noqa: F401 — фикстуры
from tests.test_webhook import fake  # noqa: F401


def test_next_occurrence():
    monday = datetime(2026, 10, 5, 12, 0)  # понедельник
    s = Schedule(weekday=0, minutes=20 * 60, kind="training")
    assert svc.next_occurrence(s, monday) == datetime(2026, 10, 5, 20, 0)  # сегодня вечером
    assert svc.next_occurrence(s, datetime(2026, 10, 5, 21, 0)) == datetime(2026, 10, 12, 20, 0)  # уже прошла
    friday = Schedule(weekday=4, minutes=19 * 60 + 30, kind="training")
    assert svc.next_occurrence(friday, monday) == datetime(2026, 10, 9, 19, 30)
    midnight = Schedule(weekday=0, minutes=24 * 60, kind="training")  # «пн 24:00» = вт 00:00
    assert svc.next_occurrence(midnight, monday) == datetime(2026, 10, 6, 0, 0)


async def test_due_schedule_games(session):
    now = config.now()
    soon = (now + timedelta(days=1)).replace(hour=20, minute=0, second=0, microsecond=0)
    far = (now + timedelta(days=5)).replace(hour=20, minute=0, second=0, microsecond=0)
    s_soon = Schedule(kind="training", weekday=soon.weekday(), minutes=20 * 60, open_days_before=2)
    s_far = Schedule(kind="training", weekday=far.weekday(), minutes=20 * 60, open_days_before=2)
    s_off = Schedule(kind="training", weekday=soon.weekday(), minutes=21 * 60, open_days_before=2, is_active=False)
    session.add_all([s_soon, s_far, s_off])
    await session.flush()
    due = await svc.due_schedule_games(session, now)
    assert [(x.id, start) for x, start in due] == [(s_soon.id, soon)]  # дальняя — рано, выключенная — нет
    await svc.create_game(session, "training", soon, None, None, schedule_id=s_soon.id)
    assert await svc.due_schedule_games(session, now) == []  # уже создана — не дублируем


async def test_scheduler_creates_and_asks_admin_when_too_few(fake, monkeypatch):  # noqa: F811
    monkeypatch.setattr(config, "admin_ids", [1])
    sm, _ = await webhook._ensure_ready()
    now = config.now()
    async with sm() as s:
        admin = await make_user(s, "Даниял", has_car=True)
        admin.telegram_id = 1
        players = [await make_user(s, n) for n in ("Арман", "Тимур", "Руслан")]
        for u in [admin, *players]:
            u.created_at -= timedelta(days=10)
        start = (now + timedelta(days=1)).replace(hour=20, minute=0, second=0, microsecond=0)
        s.add(Schedule(kind="training", weekday=start.weekday(), minutes=20 * 60, location="Арена",
                       min_players=3, open_days_before=2))
        await s.commit()

    # --- тик: тренировка создана, опрос разослан, админу — отчёт и анонс для WhatsApp
    async with sm() as s:
        await tick(webhook.make_bot(), s)
        await s.commit()
    async with sm() as s:
        games = (await s.scalars(__import__("sqlalchemy").select(Game))).all()
        assert len(games) == 1 and games[0].min_players == 3 and games[0].location == "Арена"
        game_id = games[0].id
    assert any("По расписанию создана" in m.text for m in fake.sent(1))
    invite = next(m for m in fake.sent() if "Открыт сбор" in m.text)
    assert "Нужно минимум 3 человека" in invite.text
    wa = next(m for m in fake.sent(1) if m.parse_mode is None and "Отметьтесь" in m.text)
    assert "Нужно минимум 3" in wa.text

    # --- повторный тик — без дублей
    async with sm() as s:
        await tick(webhook.make_bot(), s)
        await s.commit()
        assert len((await s.scalars(__import__("sqlalchemy").select(Game))).all()) == 1

    # --- двое «Буду», один «Не знаю», один молчит; сбор закрывается
    async with sm() as s:
        g = await s.get(Game, game_id)
        await svc.set_rsvp(s, g, await s.get(svc.User, players[0].id), "yes")
        await svc.set_rsvp(s, g, await s.get(svc.User, players[1].id), "yes")
        await svc.set_rsvp(s, g, await s.get(svc.User, players[2].id), "maybe")
        g.created_at -= timedelta(days=2)
        g.starts_at = config.now() + timedelta(hours=4)  # закрытие сбора — за 5 ч, т.е. уже
        await s.commit()
    fake.reset()
    async with sm() as s:
        await tick(webhook.make_bot(), s)
        await s.commit()
    async with sm() as s:
        g = await s.get(Game, game_id)
        assert g.status == GameStatus.OPEN and g.min_decision == "asked"  # не отменили сами
        assert await svc.open_penalty_points(s) == {admin.id: 1}  # минус молчуну — как обычно
        assert await svc.active_assignments(s, game_id) == []  # без решения не распределяем
    ask = next(m for m in fake.sent(1) if "Что делаем?" in m.text)
    assert "2 из минимум 3" in ask.text
    labels = [b.text for row in ask.reply_markup.inline_keyboard for b in row]
    assert labels[:3] == ["✅ Провести всё равно", "⏳ Подождать ещё час", "❌ Отменить"]
    assert not any("отменена" in m.text for m in fake.sent())

    # --- повторный тик — не переспрашиваем
    fake.reset()
    async with sm() as s:
        await tick(webhook.make_bot(), s)
        await s.commit()
    assert not any("Что делаем?" in m.text for m in fake.sent())

    # --- «Подождать»: напомнили молчащим и «не знаю», через час спросим снова
    async with sm() as s:
        g = await s.get(Game, game_id)
        note = await operations.decide_min(webhook.make_bot(), s, g, "wait", 1)
        assert "Ждём час" in note and g.min_recheck_at is not None
        g.min_recheck_at = config.now() - timedelta(minutes=1)
        await s.commit()
    assert any("под вопросом" in m.text for m in fake.sent(players[2].telegram_id))
    fake.reset()
    async with sm() as s:
        await tick(webhook.make_bot(), s)
        await s.commit()
    assert any("Что делаем?" in m.text for m in fake.sent(1))

    # --- Руслан передумал: «Буду» — минимум набран, тренировка состоится сама
    fake.reset()
    async with sm() as s:
        g = await s.get(Game, game_id)
        await svc.set_rsvp(s, g, await s.get(svc.User, players[2].id), "yes")
        await s.commit()
    async with sm() as s:
        await tick(webhook.make_bot(), s)
        await s.commit()
        g = await s.get(Game, game_id)
        assert g.status == GameStatus.DISTRIBUTED and g.min_decision == "keep"
    assert any("минимум набрался" in m.text for m in fake.sent(1))


async def _undermanned_game(sm, days=1):
    async with sm() as s:
        users = [await make_user(s, n) for n in ("А", "Б")]
        g = await svc.create_game(s, "training", config.now() + timedelta(days=days), None, None, min_players=5)
        for u in users:
            await svc.set_rsvp(s, g, u, "yes")
        g.min_decision = "asked"
        await s.commit()
        return g.id, users


async def test_min_decision_keep_and_cancel(fake, monkeypatch):  # noqa: F811
    monkeypatch.setattr(config, "admin_ids", [1])
    sm, _ = await webhook._ensure_ready()
    bot = webhook.make_bot()

    gid, _ = await _undermanned_game(sm)
    async with sm() as s:
        note = await operations.decide_min(bot, s, await s.get(Game, gid), "keep", 1)
        await s.commit()
        assert "Проводим" in note and (await s.get(Game, gid)).status == GameStatus.DISTRIBUTED

    gid, users = await _undermanned_game(sm, 2)
    fake.reset()
    async with sm() as s:
        await operations.decide_min(bot, s, await s.get(Game, gid), "cancel", 1)
        await s.commit()
        g = await s.get(Game, gid)
        assert g.status == GameStatus.CANCELLED and g.cancel_reason == "не набралось людей — 2 из 5"
    assert any("отменена" in m.text for m in fake.sent(users[0].telegram_id))


async def test_auto_cancel_option(session, monkeypatch):
    from aiogram import Bot

    from tests.test_bot_flow import FakeSession

    monkeypatch.setattr(config, "min_players_auto_cancel", True)
    bot = Bot("42:TEST", session=FakeSession())
    users = [await make_user(session, n) for n in ("А", "Б")]
    game = await svc.create_game(session, "training", config.now() + timedelta(days=1), None, None, min_players=3)
    for u in users:
        await svc.set_rsvp(session, game, u, "yes")
    assert await operations.check_min_players(bot, session, game) == "cancelled"
    assert game.status == GameStatus.CANCELLED


async def test_enough_players_ok(session):
    from aiogram import Bot

    from tests.test_bot_flow import FakeSession

    bot = Bot("42:TEST", session=FakeSession())
    users = [await make_user(session, n) for n in ("А", "Б", "В")]
    game = await svc.create_game(session, "training", config.now() + timedelta(days=1), None, None, min_players=3)
    for u in users:
        await svc.set_rsvp(session, game, u, "yes")
    assert await operations.check_min_players(bot, session, game) == "ok"
    assert game.status == GameStatus.OPEN and game.min_decision is None


async def test_miniapp_repeat_and_manage_schedule(team):  # noqa: F811
    tomorrow = config.now() + timedelta(days=1)
    status, res = await api(ADMIN, "create_game", date=tomorrow.date().isoformat(), minutes=20 * 60, kind="training",
                            location="Арена", min_players=6, repeat=True, open_days_before=3)
    assert status == 200, res
    assert "🔁 Дальше — " in res["note"] and "в 20:00" in res["note"]
    sched = res["state"]["schedules"]
    assert len(sched) == 1 and sched[0]["time"] == "20:00" and sched[0]["min_players"] == 6
    assert sched[0]["weekday"] == tomorrow.weekday() and sched[0]["open_days_before"] == 3
    game = res["state"]["games"][0]
    assert game["from_schedule"] is True and game["min_players"] == 6

    sid = sched[0]["id"]
    _, res = await api(ADMIN, "schedule_update", id=sid, active=False, min_players=8)
    assert res["state"]["schedules"][0]["active"] is False and res["state"]["schedules"][0]["min_players"] == 8
    # следующая неделя не создаётся, пока на паузе
    async with webhook._sessionmaker() as s:
        assert await svc.due_schedule_games(s, config.now() + timedelta(days=6)) == []
    _, res = await api(ADMIN, "schedule_delete", id=sid)
    assert res["state"]["schedules"] == [] and len(res["state"]["games"]) == 1  # игра осталась
    status, _ = await api({"id": 77, "first_name": "Чужой"}, "schedule_update", id=sid, active=True)
    assert status == 403


async def test_edit_game(team):  # noqa: F811
    fake = team  # noqa: F811
    from tests.test_miniapp import tg_user

    arman = tg_user(2, "Арман")
    await api(arman, "register", car=False)
    _, pl = await api(ADMIN, "players")
    await api(ADMIN, "player", user_id=next(p["id"] for p in pl["players"] if p["name"] == "Арман"), op="approve")

    friday = config.now() + timedelta(days=4)
    _, res = await api(ADMIN, "create_game", date=friday.date().isoformat(), minutes=23 * 60, kind="training",
                       location="Жас Оркен")
    gid = res["state"]["games"][0]["id"]
    await api(arman, "rsvp", game_id=gid, status="yes")
    async with webhook._sessionmaker() as s:  # будто напоминания уже ушли
        g = await s.get(Game, gid)
        g.personal_reminder_sent = g.group_reminder_sent = g.rsvp_nudge_sent = True
        await s.commit()

    thursday = friday - timedelta(days=1)
    fake.reset()
    status, res = await api(ADMIN, "update_game", game_id=gid, date=thursday.date().isoformat(), minutes=20 * 60,
                            kind="training", location="Арена", min_players=6)
    assert status == 200, res
    g = res["state"]["games"][0]
    assert g["time"] == "20:00" and g["date_iso"] == thursday.date().isoformat()
    assert g["location"] == "Арена" and g["min_players"] == 6
    dm = next(m for m in fake.sent(2) if "Изменения" in m.text)
    assert "23:00 →" in dm.text and "20:00" in dm.text and "Арена" in dm.text and "Минимум игроков: 6" in dm.text
    assert "*Изменения:" in res["whatsapp"]["text"] and res["whatsapp"]["url"].startswith("https://wa.me/")
    async with webhook._sessionmaker() as s:
        g = await s.get(Game, gid)
        assert not g.personal_reminder_sent and not g.group_reminder_sent and not g.rsvp_nudge_sent

    status, body = await api(ADMIN, "update_game", game_id=gid, date=thursday.date().isoformat(), minutes=20 * 60,
                             kind="training", location="Арена", min_players=6)
    assert status == 400 and "Ничего не изменилось" in body["error"]
    yesterday = (config.now() - timedelta(days=1)).date().isoformat()
    status, body = await api(ADMIN, "update_game", game_id=gid, date=yesterday, minutes=20 * 60, kind="training")
    assert status == 400 and "прошло" in body["error"]
    status, _ = await api(arman, "update_game", game_id=gid, date=thursday.date().isoformat(), minutes=19 * 60)
    assert status == 403


async def test_moved_scheduled_game_is_not_duplicated(session):
    now = config.now()
    slot = (now + timedelta(days=1)).replace(hour=20, minute=0, second=0, microsecond=0)
    x = Schedule(kind="training", weekday=slot.weekday(), minutes=20 * 60, open_days_before=2)
    session.add(x)
    await session.flush()
    (schedule, start), = await svc.due_schedule_games(session, now)
    game = await svc.create_game(session, "training", start, None, None, schedule_id=schedule.id)
    game.starts_at = start + timedelta(hours=1)  # перенесли на час
    await session.flush()
    assert await svc.due_schedule_games(session, now) == []


async def test_schedule_edit_day_time_place(team):  # noqa: F811
    tomorrow = config.now() + timedelta(days=1)
    _, res = await api(ADMIN, "create_game", date=tomorrow.date().isoformat(), minutes=20 * 60, kind="training",
                       repeat=True)
    sid = res["state"]["schedules"][0]["id"]
    _, res = await api(ADMIN, "schedule_update", id=sid, weekday=4, minutes=19 * 60 + 30, location="Жас Оркен")
    x = res["state"]["schedules"][0]
    assert x["weekday"] == 4 and x["time"] == "19:30" and x["location"] == "Жас Оркен"
    assert x["every_label"] == "каждую пятницу" and "каждую пятницу в 19:30" in res["note"]


async def test_repeat_far_away_waits_for_poll_window(team):  # noqa: F811
    """Создали в понедельник тренировку на пятницу (опрос за 2 дня) — опрос уйдёт в среду, а не сразу."""
    fake = team  # noqa: F811
    friday = config.now() + timedelta(days=4)
    before = len(fake.sent(1))
    status, res = await api(ADMIN, "create_game", date=friday.date().isoformat(), minutes=21 * 60 + 30,
                            kind="training", location="Арена", repeat=True, open_days_before=2)
    assert status == 200, res
    assert res["scheduled"] is True and "Опрос игрокам уйдёт" in res["message"] and "21:30" in res["message"]
    assert res["state"]["games"] == [] and len(res["state"]["schedules"]) == 1
    assert not any("Открыт сбор" in m.text for m in fake.sent(1)[before:])
    async with webhook._sessionmaker() as s:
        assert await svc.due_schedule_games(s, config.now()) == []
        opens = datetime.combine(friday.date(), datetime.min.time()) + timedelta(hours=21, minutes=30, days=-2)
        assert await svc.due_schedule_games(s, opens - timedelta(minutes=1)) == []
        due = await svc.due_schedule_games(s, opens + timedelta(minutes=1))
        assert len(due) == 1 and due[0][1].weekday() == friday.weekday()


async def test_tick_failure_does_not_lose_scheduled_game(fake, monkeypatch):  # noqa: F811
    """Опрос разослан — игра обязана остаться в базе, даже если дальше в том же запуске что-то упало."""
    from sqlalchemy import select

    from bot import actions

    sm, _ = await webhook._ensure_ready()
    async with sm() as s:
        now = config.now()
        start = now + timedelta(days=2) - timedelta(minutes=5)
        s.add(Schedule(kind="training", weekday=start.weekday(), minutes=start.hour * 60 + start.minute,
                       open_days_before=2))
        other = await svc.create_game(s, "game", now + timedelta(hours=10), None, None)
        other.created_at = datetime.utcnow() - timedelta(days=2)
        await s.commit()
        other_id = other.id

    async def boom(*a, **k):
        raise RuntimeError("telegram is down")

    monkeypatch.setattr(actions, "send_rsvp_nudge", boom)  # падает обработка соседней игры
    async with sm() as s:
        await tick(webhook.make_bot(), s)
        await s.commit()
    async with sm() as s:
        games = (await s.scalars(select(Game).where(Game.schedule_id.is_not(None)))).all()
        assert len(games) == 1  # игра по расписанию сохранилась
        assert (await s.get(Game, other_id)) is not None

    # рассылка опроса упала уже после создания — игра всё равно в базе, повторно не создаётся
    async with sm() as s:
        for g in (await s.scalars(select(Game).where(Game.schedule_id.is_not(None)))).all():
            await s.delete(g)
        await s.commit()
    monkeypatch.setattr(actions, "send_poll_invites", boom)
    async with sm() as s:
        await tick(webhook.make_bot(), s)
        await s.commit()
    async with sm() as s:
        assert len((await s.scalars(select(Game).where(Game.schedule_id.is_not(None)))).all()) == 1
