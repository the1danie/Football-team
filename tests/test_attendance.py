"""Кто пришёл: неявка после «Буду», пришёл без отметки, напоминание «Не знаю»."""

from datetime import timedelta

import pytest

from bot import webhook
from bot.config import config
from bot.models import Game, PenaltyStatus
from bot.scheduler import tick
from bot.services import games as svc
from tests.conftest import make_user
from tests.test_bot_flow import Harness, buttons, h  # noqa: F401
from tests.test_miniapp import ADMIN, api, team, tg_user  # noqa: F401
from tests.test_webhook import fake  # noqa: F401

pytestmark = pytest.mark.real_phases


async def _started_game(session, users, minutes_ago=20):
    game = await svc.create_game(session, "training", config.now() + timedelta(days=1), None, None)
    for u in users:
        await svc.set_rsvp(session, game, u, "yes")
    await svc.distribute_game(session, game)  # вода «до»
    game.starts_at = config.now() - timedelta(minutes=minutes_ago)
    await session.flush()
    return game


async def test_no_show_and_walk_in(session, rng):
    driver = await make_user(session, "Водитель", has_car=True)
    a, b, c = await make_user(session, "А"), await make_user(session, "Б"), await make_user(session, "В")
    walk_in = await make_user(session, "Без отметки")
    game = await _started_game(session, [driver, a, b, c])
    water_holder = (await svc.active_assignments(session, game.id))[0].user

    # водоноса не было: вода не засчитывается, минус
    res = await svc.set_attendance(session, game, water_holder, False)
    assert res.penalty_total == 1
    assert await svc.user_assignments(session, game.id, water_holder.id) == []
    assert water_holder.id in await svc.absent_user_ids(session, game.id)

    # пришёл без отметки — теперь «Буду» и в списке на обязанности «после»
    await svc.set_attendance(session, game, walk_in, True)
    assert await svc.get_rsvp(session, game.id, walk_in.id) == "yes"

    after = await svc.distribute_game(session, game, rng, phase="after")
    takers = {x.user_id for x in after.assignments}
    assert water_holder.id not in takers
    assert walk_in.id in takers or len(takers) == 3

    # ошиблись — вернули: минус снят
    res = await svc.set_attendance(session, game, water_holder, True)
    assert res.penalty_removed
    assert await svc.open_penalty_points(session, [water_holder.id]) == {}


async def test_absent_after_duty_is_reassigned(session, rng):
    users = [await make_user(session, n, has_car=True) for n in ("А", "Б", "В", "Г", "Д")]
    game = await _started_game(session, users, minutes_ago=70)
    await svc.distribute_game(session, game, rng, phase="after")
    balls = next(x for x in await svc.active_assignments(session, game.id) if x.duty.code == "balls")
    old = balls.user
    res = await svc.set_attendance(session, game, old, False)
    assert len(res.reassigned) == 1 and res.reassigned[0].new_user.id != old.id
    new_balls = next(x for x in await svc.active_assignments(session, game.id) if x.duty.code == "balls")
    assert new_balls.user_id != old.id


async def test_admin_gets_attendance_list_and_toggles(h: Harness):  # noqa: F811
    tg = h.fake
    await h.register(1, "Даниял", car=True)
    await h.register(2, "Арман", car=False)
    await h.register(3, "Тимур", car=False)
    async with h.sm() as s:
        users = [await svc.get_user_by_tg(s, i) for i in (1, 2, 3)]
        game = await _started_game(s, users)
        await s.commit()
        gid, arman = game.id, users[1]

    tg.reset()
    async with h.sm() as s:
        await tick(h.bot, s)
        await s.commit()
    ask = next(m for m in tg.sent(1) if "Кто пришёл?" in m.text)
    labels = [b.text for b in buttons(ask.reply_markup)]
    assert "✅ Арман" in labels and "✅ Тимур" in labels
    # повторно не шлём
    tg.reset()
    async with h.sm() as s:
        await tick(h.bot, s)
        await s.commit()
    assert not any("Кто пришёл?" in m.text for m in tg.sent())

    await h.click(1, f"att:{gid}:{arman.id}:0")
    assert any("но не пришёл" in m.text for m in tg.sent(2))
    async with h.sm() as s:
        assert (await svc.open_penalty_points(s, [arman.id]))[arman.id] == 1
    await h.click(1, f"att:{gid}:{arman.id}:1")
    assert any("Минус за неявку" in m.text and "снят" in m.text for m in tg.sent(2))
    # игроку кнопки недоступны
    await h.click(2, f"att:{gid}:{arman.id}:0")
    async with h.sm() as s:
        assert await svc.open_penalty_points(s, [arman.id]) == {}


async def test_miniapp_attendance(team):  # noqa: F811
    arman = tg_user(2, "Арман")
    await api(arman, "register", car=False)
    _, pl = await api(ADMIN, "players")
    arman_id = next(p["id"] for p in pl["players"] if p["name"] == "Арман")
    await api(ADMIN, "player", user_id=arman_id, op="approve")
    soon = config.now() + timedelta(days=1)
    _, res = await api(ADMIN, "create_game", date=soon.date().isoformat(), minutes=20 * 60, kind="training")
    gid = res["state"]["games"][0]["id"]
    await api(arman, "rsvp", game_id=gid, status="yes")

    status, body = await api(ADMIN, "attendance", game_id=gid, user_id=arman_id, present=False)
    assert status == 400 and "с часа до начала" in body["error"]  # рано

    async with webhook._sessionmaker() as s:
        (await s.get(Game, gid)).starts_at = config.now() - timedelta(minutes=10)
        await s.commit()
    _, res = await api(ADMIN, "state")
    assert res["state"]["games"][0]["attendance_open"] is True
    _, res = await api(ADMIN, "attendance", game_id=gid, user_id=arman_id, present=False)
    g = res["state"]["games"][0]
    absent = next(u for u in g["participants"]["yes"] if u["id"] == arman_id)
    assert absent["attended"] is False
    _, me = await api(arman, "player_stats", user_id=arman_id)
    assert me["minuses"][0]["reason"] == "сказал «Буду», но не пришёл"
    status, _ = await api(arman, "attendance", game_id=gid, user_id=arman_id, present=True)
    assert status == 403


async def test_maybe_gets_nudge(fake, monkeypatch):  # noqa: F811
    monkeypatch.setattr(config, "admin_ids", [1])
    sm, _ = await webhook._ensure_ready()
    async with sm() as s:
        doubter = await make_user(s, "Сомневающийся")
        doubter.created_at -= timedelta(days=3)
        game = await svc.create_game(s, "training", config.now() + timedelta(hours=7), None, None)
        game.created_at -= timedelta(days=2)
        await svc.set_rsvp(s, game, doubter, "maybe")
        await s.commit()
    fake.reset()
    async with sm() as s:
        await tick(webhook.make_bot(), s)
        await s.commit()
    nudge = next(m for m in fake.sent(doubter.telegram_id) if "Ты ответил «Не знаю»" in m.text)
    assert "Определись" in nudge.text and "минус" not in nudge.text
    async with sm() as s:
        assert await svc.open_penalty_points(s) == {}
    assert PenaltyStatus.OPEN  # импорт используется
