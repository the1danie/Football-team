"""Дисциплина: «не выполнил» (−2), «отдал другому» (1-й раз бесплатно, 2-й −1, дальше −3), пороги 3/6/9."""

from datetime import timedelta

from sqlalchemy import select

from bot import discipline, webhook
from bot.config import config
from bot.models import Game, Penalty, PenaltyReason, PenaltyStatus, UserStatus
from bot.services import games as svc
from tests.test_miniapp import ADMIN, api, team, tg_user  # noqa: F401 — фикстура team
from tests.test_webhook import fake  # noqa: F401


def test_rules_numbers():
    assert [discipline.transfer_points(n) for n in (1, 2, 3, 4)] == [0, 1, 3, 3]
    assert [discipline.burpees(n) for n in (2, 3, 5, 6, 8)] == [None, 25, 25, 50, 50]
    assert "исключение" in discipline.level_text(9) and "25 бёрпи" in discipline.level_text(3)


async def _member(user):
    await api(user, "register", car=True)
    _, pl = await api(ADMIN, "players")
    uid = next(p["id"] for p in pl["players"] if p["name"] == user["first_name"])
    await api(ADMIN, "player", user_id=uid, op="approve")
    return uid


async def _game(days, *players):
    day = (config.now() + timedelta(days=days)).date().isoformat()
    _, res = await api(ADMIN, "create_game", date=day, minutes=20 * 60, kind="training")
    gid = max(g["id"] for g in res["state"]["games"])
    for p in players:
        await api(p, "rsvp", game_id=gid, status="yes")
    await api(ADMIN, "distribute", game_id=gid)
    return gid


async def _open_points(uid):
    async with webhook._sessionmaker() as s:
        return (await svc.open_penalty_points(s, [uid])).get(uid, 0)


async def test_not_done_with_rescuer_and_undo(team):  # noqa: F811
    fake = team  # noqa: F811
    meir, arman = tg_user(2, "Мейр"), tg_user(3, "Арман")
    meir_id = await _member(meir)
    arman_id = await _member(arman)
    gid = await _game(1, meir, arman)
    async with webhook._sessionmaker() as s:
        water = next(a for a in await svc.active_assignments(s, gid) if a.duty.name == "Вода")
        water_duty, holder = water.duty_id, water.user_id
        (await s.get(Game, gid)).starts_at = config.now() - timedelta(minutes=30)  # тренировка идёт
        await s.commit()
    other = arman_id if holder == meir_id else meir_id
    holder_tg = 2 if holder == meir_id else 3
    status, res = await api(ADMIN, "duty_failed", game_id=gid, duty_id=water_duty, done_by=other)
    assert status == 200, res
    duty = next(d for d in res["state"]["games"][0]["duties"] if d["duty_id"] == water_duty)
    assert duty["failed_by"]["id"] == holder and duty["user"]["id"] == other
    assert await _open_points(holder) == 2
    assert any("не выполнил обязанность" in m.text and "−2" in m.text for m in fake.sent(holder_tg))
    # не админ отметить не может
    assert (await api(meir, "duty_failed", game_id=gid, duty_id=water_duty))[0] == 403
    # ошибочно — отменяем: минус снят, обязанность снова за ним
    _, res = await api(ADMIN, "duty_failed_undo", game_id=gid, duty_id=water_duty)
    duty = next(d for d in res["state"]["games"][0]["duties"] if d["duty_id"] == water_duty)
    assert duty["failed_by"] is None and duty["user"]["id"] == holder
    assert await _open_points(holder) == 0


async def test_giving_away_escalates_and_thresholds(team):  # noqa: F811
    fake = team  # noqa: F811
    meir, arman, timur = tg_user(2, "Мейр"), tg_user(3, "Арман"), tg_user(4, "Тимур")
    meir_id = await _member(meir)
    await _member(arman)
    await _member(timur)

    async def give_away(days):
        gid = await _game(days, meir, arman, timur)
        async with webhook._sessionmaker() as s:
            had = await svc.user_assignments(s, gid, meir_id)
        if not had:  # обязанность могла достаться другим — пусть Мейр возьмёт воду
            async with webhook._sessionmaker() as s:
                water = next(a for a in await svc.active_assignments(s, gid) if a.duty.name == "Вода")
            await api(ADMIN, "assign", game_id=gid, duty_id=water.duty_id, user_id=meir_id)
        await api(meir, "rsvp", game_id=gid, status="no")  # передумал — обязанность ушла другому

    await give_away(1)
    assert await _open_points(meir_id) == 0  # 1-й раз — бесплатно
    await give_away(2)
    assert await _open_points(meir_id) == 1  # 2-й — −1
    await give_away(3)
    assert await _open_points(meir_id) == 4  # 3-й — −3 → всего 4: порог 25 бёрпи пройден
    assert any("25 бёрпи" in m.text for m in fake.sent(2))
    assert any("Мейр" in m.text and "25 бёрпи" in m.text for m in fake.sent(1))  # админу
    _, st = await api(meir, "state")
    assert "25 бёрпи" in st["state"]["user"]["level"]
    gid = await _game(4, meir, arman)
    _, st = await api(ADMIN, "state")
    g = next(x for x in st["state"]["games"] if x["id"] == gid)
    assert next(u for u in g["participants"]["yes"] if u["id"] == meir_id)["burpees"] == 25

    # −9 — исключение из команды
    async with webhook._sessionmaker() as s:
        user = await svc.get_user_by_tg(s, 2)
        await discipline.penalize(_Bot(), s, user, None, 5, PenaltyReason.NOT_DONE)
        await s.commit()
    async with webhook._sessionmaker() as s:
        assert (await svc.get_user_by_tg(s, 2)).status == UserStatus.BLOCKED
        rows = (await s.scalars(select(Penalty).where(Penalty.user_id == meir_id))).all()
        assert sum(p.points for p in rows if p.status == PenaltyStatus.OPEN) == 9
    _, st = await api(meir, "state")
    assert st["state"]["access"] == "blocked"


class _Bot:
    """Заглушка бота для прямого вызова penalize (сообщения не проверяем)."""

    async def send_message(self, *a, **k):
        return None

    async def me(self):
        return None
