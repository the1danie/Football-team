"""Дисциплина: «не выполнил» (−2), «отдал другому» (1-й раз бесплатно, 2-й −1, 3-й −2, …), пороги 3/6/9."""

from datetime import timedelta

from sqlalchemy import select

from bot import discipline, webhook
from bot.config import config
from bot.models import Game, Penalty, PenaltyReason, PenaltyStatus, UserStatus
from bot.services import games as svc
from tests.test_miniapp import ADMIN, api, team, tg_user  # noqa: F401 — фикстура team
from tests.test_webhook import fake  # noqa: F401


def test_rules_numbers():
    assert [discipline.transfer_points(n) for n in (1, 2, 3, 4, 5)] == [0, 1, 2, 3, 4]
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
    assert await _open_points(meir_id) == 3  # 3-й — −2 → всего 3: порог 25 бёрпи пройден
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
        await discipline.penalize(_Bot(), s, user, None, 6, PenaltyReason.NOT_DONE)
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


async def test_burpees_done_resets_minuses(team):  # noqa: F811
    fake = team  # noqa: F811
    meir_id = await _member(tg_user(2, "Мейр"))
    coach_id = await _member(tg_user(5, "Серик"))
    await api(ADMIN, "player", user_id=coach_id, op="staff", name="Тренер")
    async with webhook._sessionmaker() as s:
        meir = await svc.get_user_by_tg(s, 2)
        await discipline.add_penalty(s, meir, None, 4, PenaltyReason.NOT_DONE)
        await s.commit()
    _, p = await api(ADMIN, "player_detail", user_id=meir_id)
    assert p["burpees"] == 25
    assert (await api(tg_user(2, "Мейр"), "burpees_done", user_id=meir_id))[0] == 403  # сам себе — нельзя
    status, res = await api(tg_user(5, "Серик"), "burpees_done", user_id=meir_id)  # тренер может
    assert status == 200 and "обнулены" in res["note"]
    assert await _open_points(meir_id) == 0
    assert any("25 бёрпи засчитаны" in m.text for m in fake.sent(2))
    status, body = await api(ADMIN, "burpees_done", user_id=meir_id)
    assert status == 400 and "нет долга" in body["error"]
    _, log = await api(ADMIN, "audit")
    assert any("сделал 25 бёрпи" in e["text"] for e in log["entries"])


async def test_balance_is_help_minus_minuses(team):  # noqa: F811
    meir_id = await _member(tg_user(2, "Мейр"))
    async with webhook._sessionmaker() as s:
        meir = await svc.get_user_by_tg(s, 2)
        await discipline.add_penalty(s, meir, None, 2, PenaltyReason.NOT_DONE)
        await s.commit()
    _, st = await api(tg_user(2, "Мейр"), "state")
    u = st["state"]["user"]
    assert (u["help"], u["minus_total"], u["balance"]) == (0, 2, -2)
    _, stats = await api(ADMIN, "stats")
    row = next(r for r in stats["board"] if r["id"] == meir_id)
    assert row["balance"] == row["duties"] - row["minuses"] == -2


async def test_burpees_clear_minuses_in_rating_too(team):  # noqa: F811
    """Бёрпи сделал — в рейтинге (Итог и Минусы) этих минусов тоже больше нет."""
    meir_id = await _member(tg_user(2, "Мейр"))
    async with webhook._sessionmaker() as s:
        meir = await svc.get_user_by_tg(s, 2)
        await discipline.add_penalty(s, meir, None, 3, PenaltyReason.NOT_DONE)
        await s.commit()
    _, stats = await api(ADMIN, "stats")
    assert next(r for r in stats["board"] if r["id"] == meir_id)["balance"] == -3
    await api(ADMIN, "burpees_done", user_id=meir_id)
    _, stats = await api(ADMIN, "stats")
    row = next(r for r in stats["board"] if r["id"] == meir_id)
    assert (row["minuses"], row["balance"]) == (0, 0)
    _, st = await api(tg_user(2, "Мейр"), "state")
    assert (st["state"]["user"]["minuses"], st["state"]["user"]["balance"]) == (0, 0)


async def test_old_burpees_fixed_once(team):  # noqa: F811
    """Бёрпи, отмеченные до исправления (минусы были «отработаны»), убираются из рейтинга при запуске."""
    from datetime import datetime

    from bot import db
    from bot.models import AuditLog, Setting

    meir_id = await _member(tg_user(2, "Мейр"))
    async with webhook._sessionmaker() as s:
        old = datetime.utcnow() - timedelta(days=5)
        s.add_all([
            Penalty(user_id=meir_id, points=1, status=PenaltyStatus.REDEEMED, redeemed_game_id=None,
                    created_at=old - timedelta(days=1)),
            Penalty(user_id=meir_id, points=2, status=PenaltyStatus.REDEEMED, created_at=old),
            Penalty(user_id=meir_id, points=1, status=PenaltyStatus.REDEEMED, created_at=old),
            AuditLog(actor_name="Даниял", text="💪 Мейр: сделал 25 бёрпи — минусы обнулены (было −3)",
                     created_at=old + timedelta(hours=1)),
        ])
        await s.delete(await s.get(Setting, db.BURPEES_STATUS_KEY))
        await s.commit()
    await db.init_db(webhook._engine)
    async with webhook._sessionmaker() as s:
        statuses = sorted(p.status for p in (await s.scalars(select(Penalty).where(Penalty.user_id == meir_id))).all())
    assert statuses == ["burpees", "burpees", "redeemed"]
    async with webhook._sessionmaker() as s:
        assert (await discipline.burpee_rounds(s, [meir_id])) == {meir_id: 1}


async def test_burpees_grow_each_time_within_3_months(team):  # noqa: F811
    """1-й раз 25, 2-й раз 50, 3-й 75; через 3 месяца без бёрпи — снова 25."""
    from datetime import datetime

    meir_id = await _member(tg_user(2, "Мейр"))

    async def debt_and_due():
        async with webhook._sessionmaker() as s:
            meir = await svc.get_user_by_tg(s, 2)
            await discipline.add_penalty(s, meir, None, 3, PenaltyReason.NOT_DONE)
            await s.commit()
        _, p = await api(ADMIN, "player_detail", user_id=meir_id)
        return p["burpees"]

    assert await debt_and_due() == 25
    await api(ADMIN, "burpees_done", user_id=meir_id)
    assert await debt_and_due() == 50
    _, st = await api(tg_user(2, "Мейр"), "state")
    info = st["state"]["user"]["burpee_info"]
    assert (info["rounds"], info["next"]) == (1, 50) and info["reset"]
    _, p = await api(ADMIN, "player_detail", user_id=meir_id)
    assert p["burpee_info"]["next"] == 50
    assert "50 бёрпи (2-й раз" in st["state"]["user"]["level"]
    status, res = await api(ADMIN, "burpees_done", user_id=meir_id)
    assert "50 бёрпи" in res["note"]
    assert await debt_and_due() == 75
    await api(ADMIN, "burpees_done", user_id=meir_id)
    async with webhook._sessionmaker() as s:  # прошло 3 месяца
        for p in (await s.scalars(select(Penalty).where(Penalty.status == PenaltyStatus.BURPEES))).all():
            p.burpees_at = datetime.utcnow() - timedelta(days=91)
        await s.commit()
    assert await debt_and_due() == 25


async def test_removed_player_not_in_rating(team):  # noqa: F811
    temir_id = await _member(tg_user(6, "Темир"))
    _, stats = await api(ADMIN, "stats")
    assert temir_id in {r["id"] for r in stats["board"]}
    await api(ADMIN, "player", user_id=temir_id, op="block")
    _, stats = await api(ADMIN, "stats")
    assert temir_id not in {r["id"] for r in stats["board"]}
    assert temir_id not in {r["id"] for r in stats["team"]}
