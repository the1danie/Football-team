"""Удалённый из команды игрок пропадает из списков ответов игры и из счётчика «Ответили N из M»."""

from datetime import timedelta

from bot.config import config
from bot.models import GameParticipant, User, UserStatus
from bot import webhook
from tests.test_miniapp import ADMIN, api, team, tg_user  # noqa: F401
from tests.test_webhook import fake  # noqa: F401


async def _approve(user):
    await api(user, "register", car=False, name=user["first_name"])
    _, pl = await api(ADMIN, "players")
    uid = next(p["id"] for p in pl["players"] if p["name"] == user["first_name"])
    await api(ADMIN, "player", user_id=uid, op="approve")
    return uid


async def test_blocked_player_hidden_from_game(team):  # noqa: F811
    sasha, chira = tg_user(2, "Александр"), tg_user(3, "Чира")
    sasha_id = await _approve(sasha)
    await _approve(chira)
    day = (config.now() + timedelta(days=2)).date().isoformat()
    _, res = await api(ADMIN, "create_game", date=day, minutes=19 * 60, kind="training")
    gid = res["state"]["games"][0]["id"]
    await api(sasha, "rsvp", game_id=gid, status="maybe")
    await api(chira, "rsvp", game_id=gid, status="yes")

    await api(ADMIN, "player", user_id=sasha_id, op="block")
    _, st = await api(ADMIN, "state")
    g = next(x for x in st["state"]["games"] if x["id"] == gid)
    names = [u["name"] for s in g["participants"].values() for u in s]
    assert "Александр" not in names and "Чира" in names
    assert g["team_count"] == 2  # админ + Чира

    # ответ в базе остался (вернут в команду — ничего не потеряется), но в списках его нет
    async with webhook._sessionmaker() as s:
        assert await s.get(GameParticipant, (gid, sasha_id)) is not None
        assert (await s.get(User, sasha_id)).status == UserStatus.BLOCKED
