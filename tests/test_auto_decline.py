"""Закрытие сбора: «Не знаю» и молчащие → «Не буду», вода — только среди «Буду»; напоминание за 2 ч."""

from datetime import datetime, timedelta

from bot import webhook
from bot.config import Config, config
from bot.models import Game, GameParticipant, GameStatus, Rsvp
from bot.scheduler import tick
from bot.services import games as svc
from tests.test_miniapp import ADMIN, api, team, tg_user  # noqa: F401 — фикстура team
from tests.test_webhook import fake  # noqa: F401


def test_reminder_two_hours_before_close(monkeypatch):
    monkeypatch.delenv("RSVP_REMINDER_HOURS", raising=False)
    assert Config().rsvp_reminder_hours == 2


async def _member(user):
    await api(user, "register", car=True)
    _, pl = await api(ADMIN, "players")
    uid = next(p["id"] for p in pl["players"] if p["name"] == user["first_name"])
    await api(ADMIN, "player", user_id=uid, op="approve")
    return uid


async def test_close_converts_maybe_and_silent(team):  # noqa: F811
    fake = team  # noqa: F811
    arman, timur, ruslan = tg_user(2, "Арман"), tg_user(3, "Тимур"), tg_user(4, "Руслан")
    ids = {u["first_name"]: await _member(u) for u in (arman, timur, ruslan)}
    day = (config.now() + timedelta(days=1)).date().isoformat()
    _, res = await api(ADMIN, "create_game", date=day, minutes=20 * 60, kind="training")
    gid = res["state"]["games"][0]["id"]
    await api(ADMIN, "rsvp", game_id=gid, status="yes")
    await api(arman, "rsvp", game_id=gid, status="yes")
    await api(timur, "rsvp", game_id=gid, status="maybe")  # Руслан молчит
    async with webhook._sessionmaker() as s:
        for u in await svc.all_users(s):
            u.created_at = datetime.utcnow() - timedelta(days=10)
        game = await s.get(Game, gid)
        game.created_at = datetime.utcnow() - timedelta(days=2)
        game.starts_at = config.now() + timedelta(hours=2, minutes=50)  # сбор закрылся (за 3 ч до начала)
        await s.commit()
    async with webhook._sessionmaker() as s:
        await tick(webhook.make_bot(), s)
        await s.commit()

    async with webhook._sessionmaker() as s:
        game = await s.get(Game, gid)
        assert game.auto_declined and game.status == GameStatus.DISTRIBUTED
        by = await svc.participants_by_status(s, gid)
        assert {u.name for u in by[Rsvp.YES]} == {"Даниял", "Арман"}
        assert by[Rsvp.MAYBE] == [] and "Тимур" in {u.name for u in by[Rsvp.NO]}
        assert "Руслан" not in {u.name for u in by[Rsvp.NO]}  # молчал — не «ответ»
        assert [u.name for u in await svc.non_responders(s, game)] == ["Руслан"]  # но в «молчали»
        assert (await s.get(GameParticipant, (gid, ids["Руслан"]))).auto_from == "silent"
        assert await svc.get_rsvp(s, gid, ids["Руслан"]) == Rsvp.NO
        holders = {a.user.name for a in await svc.active_assignments(s, gid)}
        assert holders <= {"Даниял", "Арман"}  # вода — только среди «Буду»
        assert (await svc.open_penalty_points(s, [ids["Руслан"]]))[ids["Руслан"]] == 1  # минус за молчание
        assert not (await svc.open_penalty_points(s, [ids["Тимур"]])).get(ids["Тимур"])  # «Не знаю» — без минуса
    assert any("не определился — записан «Не буду»" in m.text for m in fake.sent(3))
    assert any("не ответил — записан «Не буду»" in m.text for m in fake.sent(4))
    _, st = await api(ruslan, "state")
    g = st["state"]["games"][0]
    assert g["my_rsvp"] == "no" and g["auto_declined"] is True
    # Руслан передумал — сам уже не может, только через админа
    status, res = await api(ruslan, "rsvp", game_id=gid, status="yes")
    assert "отправлен админу" in res["note"]

    # аналитика помнит, кто молчал, а кто был «Не знаю»
    async with webhook._sessionmaker() as s:
        game = await s.get(Game, gid)
        game.starts_at = config.now() - timedelta(hours=5)
        game.status = GameStatus.FINISHED
        await s.commit()
    _, rep = await api(ADMIN, "attendance_report", period="all")
    rows = {r["name"]: r for r in rep["rows"]}
    assert rows["Руслан"]["silent"] == 1 and rows["Тимур"]["maybe"] == 1


async def test_close_waits_for_min_players_decision(team):  # noqa: F811
    timur = tg_user(3, "Тимур")
    await _member(timur)
    day = (config.now() + timedelta(days=1)).date().isoformat()
    _, res = await api(ADMIN, "create_game", date=day, minutes=20 * 60, kind="training", min_players=5)
    gid = res["state"]["games"][0]["id"]
    await api(timur, "rsvp", game_id=gid, status="maybe")
    async with webhook._sessionmaker() as s:
        game = await s.get(Game, gid)
        game.created_at = datetime.utcnow() - timedelta(days=2)
        game.starts_at = config.now() + timedelta(hours=2, minutes=50)
        await s.commit()
    async with webhook._sessionmaker() as s:
        await tick(webhook.make_bot(), s)
        await s.commit()
    async with webhook._sessionmaker() as s:
        game = await s.get(Game, gid)
        assert not game.auto_declined and game.min_decision == "asked"  # админ решает — «Не знаю» не трогаем
        assert await svc.get_rsvp(s, gid, (await svc.get_user_by_tg(s, 3)).id) == Rsvp.MAYBE
