"""Разовая игра заранее: создать сейчас, а опрос — за N дней до игры (бот выложит сам)."""

from datetime import timedelta

from bot import webhook
from bot.config import config
from bot.models import Game, GameStatus
from bot.scheduler import tick
from tests.test_discipline import _member
from tests.test_miniapp import ADMIN, api, team, tg_user  # noqa: F401 — фикстура team
from tests.test_webhook import fake  # noqa: F401


async def test_planned_game_published_later(team):  # noqa: F811
    fake = team  # noqa: F811
    rustam = tg_user(2, "Рустам")
    await _member(rustam)
    day = (config.now() + timedelta(days=5)).date().isoformat()
    fake.reset()
    status, res = await api(ADMIN, "create_game", date=day, minutes=17 * 60, kind="game", opponent="Барыс", publish_days=2)
    assert status == 200 and res["planned"] and "Опрос игрокам уйдёт" in res["message"]
    assert not [m for m in fake.sent(2)]  # игроку — ничего
    assert res["state"]["games"] == [] and res["state"]["planned"][0]["opponent"] == "Барыс"
    gid = res["state"]["planned"][0]["id"]
    _, st = await api(rustam, "state")
    assert st["state"]["games"] == [] and "planned" not in st["state"]  # игрок не видит
    assert (await api(rustam, "rsvp", game_id=gid, status="yes"))[0] == 400  # и отметиться нельзя
    assert (await api(rustam, "publish_now", game_id=gid))[0] == 403

    # изменили дату — опрос сдвинулся вместе с игрой
    day2 = (config.now() + timedelta(days=6)).date().isoformat()
    await api(ADMIN, "update_game", game_id=gid, date=day2, minutes=17 * 60, kind="game", opponent="Барыс")
    async with webhook._sessionmaker() as s:
        g = await s.get(Game, gid)
        assert g.status == GameStatus.PLANNED and g.starts_at - g.publish_at == timedelta(days=2)
        g.publish_at = config.now() - timedelta(minutes=1)  # пора
        await s.commit()
    fake.reset()
    async with webhook._sessionmaker() as s:
        await tick(webhook.make_bot(), s)  # планировщик сам выкладывает
        await s.commit()
    async with webhook._sessionmaker() as s:
        assert (await s.get(Game, gid)).status == GameStatus.OPEN
    assert any("против «Барыс»" in m.text for m in fake.sent(2))  # игроку ушёл опрос
    assert any(m.text.startswith("📣 Выложена") for m in fake.sent(1))  # админу — что выложена, и анонс
    _, st = await api(rustam, "state")
    assert [g["id"] for g in st["state"]["games"]] == [gid]


async def test_planned_delete_and_too_late(team):  # noqa: F811
    day = (config.now() + timedelta(days=3)).date().isoformat()
    _, res = await api(ADMIN, "create_game", date=day, minutes=17 * 60, kind="training", publish_days=1)
    gid = res["state"]["planned"][0]["id"]
    _, res = await api(ADMIN, "cancel", game_id=gid)
    assert "опрос не уходил" in res["note"] and res["state"]["planned"] == []
    # «за N дней» уже прошло (игра через 2 дня, «за 5») — опрос уходит сразу
    soon = (config.now() + timedelta(days=2)).date().isoformat()
    status, res = await api(ADMIN, "create_game", date=soon, minutes=20 * 60, kind="training", publish_days=5)
    assert status == 200 and not res.get("planned") and res["state"]["games"]
