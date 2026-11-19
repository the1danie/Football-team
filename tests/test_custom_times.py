"""Своё время сбора и закрытия опроса у конкретной игры (по умолчанию: игра — сбор за час, тренировка — за 30 мин)."""

from datetime import timedelta

from bot import texts, webhook
from bot.config import config
from bot.deadlines import rsvp_deadline
from bot.models import Game
from bot.scheduler import tick
from tests.test_discipline import _member
from tests.test_miniapp import ADMIN, api, team, tg_user  # noqa: F401 — фикстура team
from tests.test_webhook import fake  # noqa: F401


async def test_gather_defaults_and_custom(team):  # noqa: F811
    day = (config.now() + timedelta(days=3)).date().isoformat()
    _, res = await api(ADMIN, "create_game", date=day, minutes=21 * 60 + 30, kind="game")
    g = res["state"]["games"][0]
    assert g["gather_time"] == "20:30"  # игра — за час
    _, res = await api(ADMIN, "create_game", date=day, minutes=19 * 60, kind="training")
    t = next(x for x in res["state"]["games"] if x["kind"] == "training")
    assert t["gather_time"] == "18:30"  # тренировка — за 30 мин
    _, res = await api(ADMIN, "update_game", game_id=g["id"], date=day, minutes=21 * 60 + 30, kind="game", gather_minutes=90)
    assert next(x for x in res["state"]["games"] if x["id"] == g["id"])["gather_time"] == "20:00"
    _, res = await api(ADMIN, "update_game", game_id=g["id"], date=day, minutes=21 * 60 + 30, kind="game", gather_minutes=0)
    assert next(x for x in res["state"]["games"] if x["id"] == g["id"])["gather_time"] is None  # без сбора
    assert (await api(ADMIN, "update_game", game_id=g["id"], date=day, minutes=21 * 60 + 30, kind="game",
                      gather_minutes=500))[0] == 400


async def test_custom_rsvp_deadline_closes_poll_then(team):  # noqa: F811
    fake = team  # noqa: F811
    rustam, timur = tg_user(2, "Рустам"), tg_user(3, "Тимур")
    await _member(rustam)
    await _member(timur)
    day = (config.now() + timedelta(days=2)).date().isoformat()
    close = (config.now() + timedelta(hours=5)).replace(second=0, microsecond=0)
    status, res = await api(ADMIN, "create_game", date=day, minutes=17 * 60, kind="game",
                            rsvp_deadline=close.isoformat(timespec="minutes"))
    assert status == 200, res
    gid = res["state"]["games"][0]["id"]
    async with webhook._sessionmaker() as s:
        assert rsvp_deadline(await s.get(Game, gid)) == close
    assert any(f"{texts.fmt_time(close)}" in m.text for m in fake.sent(2))  # в опросе — новое время закрытия
    # после начала нельзя, в прошлом нельзя
    start = (config.now() + timedelta(days=2)).replace(hour=17, minute=0)
    assert (await api(ADMIN, "update_game", game_id=gid, date=day, minutes=17 * 60, kind="game",
                      rsvp_deadline=(start + timedelta(hours=1)).isoformat(timespec="minutes")))[0] == 400
    assert (await api(ADMIN, "update_game", game_id=gid, date=day, minutes=17 * 60, kind="game",
                      rsvp_deadline=(config.now() - timedelta(hours=1)).isoformat(timespec="minutes")))[0] == 400
    # наступило своё время — бот закрывает сбор (минус молчащим), хотя до игры ещё больше суток
    await api(rustam, "rsvp", game_id=gid, status="yes")
    async with webhook._sessionmaker() as s:
        g = await s.get(Game, gid)
        g.rsvp_deadline_at = config.now() - timedelta(minutes=1)
        g.created_at -= timedelta(days=1)
        await s.commit()
    async with webhook._sessionmaker() as s:
        await tick(webhook.make_bot(), s)
        await s.commit()
    async with webhook._sessionmaker() as s:
        g = await s.get(Game, gid)
        assert g.penalties_applied and g.status == "distributed"
    # сбор закрыт — время закрытия больше не меняется
    status, res = await api(ADMIN, "update_game", game_id=gid, date=day, minutes=17 * 60, kind="game",
                            rsvp_deadline=(config.now() + timedelta(hours=3)).isoformat(timespec="minutes"))
    assert status == 400 and "уже закрыт" in res["error"]
