"""Повторяющиеся тренировки и отмена, если не набралось минимума."""

from datetime import datetime, timedelta

from bot import webhook
from bot.config import config
from bot.models import Game, GameStatus, Schedule
from bot.scheduler import tick
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


async def test_scheduler_creates_and_cancels_when_too_few(fake, monkeypatch):  # noqa: F811
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
    assert "Нужно минимум 3 человека, иначе отменим" in invite.text
    wa = next(m for m in fake.sent(1) if m.parse_mode is None and "Отметьтесь в боте" in m.text)
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
        assert g.status == GameStatus.CANCELLED
        assert g.cancel_reason == "не набралось людей — 2 из 3"
        assert await svc.open_penalty_points(s) == {admin.id: 1}  # минус молчуну — до отмены
        assert await svc.active_assignments(s, game_id) == []  # не распределяли
    notified = {m.chat_id for m in fake.sent() if "отменена: не набралось людей — 2 из 3" in m.text}
    assert {players[0].telegram_id, players[1].telegram_id, players[2].telegram_id} <= notified
    assert any("отменена" in m.text and m.parse_mode is None for m in fake.sent(1))  # текст для WhatsApp


async def test_enough_players_not_cancelled(session):
    from bot import operations
    from tests.test_bot_flow import FakeSession
    from aiogram import Bot

    bot = Bot("42:TEST", session=FakeSession())
    users = [await make_user(session, n) for n in ("А", "Б", "В")]
    game = await svc.create_game(session, "training", config.now() + timedelta(days=1), None, None, min_players=3)
    for u in users:
        await svc.set_rsvp(session, game, u, "yes")
    assert await operations.cancel_if_too_few(bot, session, game) is False
    assert game.status == GameStatus.OPEN
    game.min_players = None
    assert await operations.cancel_if_too_few(bot, session, game) is False


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
