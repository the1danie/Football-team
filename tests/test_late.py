"""Опоздания: на 10+ минут от начала — −1; игрок может предупредить «Опаздываю»; форс-мажор — без минуса."""

from datetime import timedelta

from sqlalchemy import select

from bot import discipline, texts, webhook
from bot.config import config
from bot.models import Game, GameParticipant, Penalty, PenaltyReason, PenaltyStatus
from tests.test_discipline import _member, _open_points
from tests.test_miniapp import ADMIN, api, team, tg_user  # noqa: F401 — фикстура team
from tests.test_webhook import fake  # noqa: F401


def test_late_points():
    assert [discipline.late_points(m) for m in (0, 5, 9, 10, 19, 20, 29, 30, 45, None)] == [0, 0, 0, 1, 1, 1, 1, 1, 1, 0]


async def _game_starting(minutes_from_now, *players):
    day = (config.now() + timedelta(days=1)).date().isoformat()
    _, res = await api(ADMIN, "create_game", date=day, minutes=20 * 60, kind="training")
    gid = max(g["id"] for g in res["state"]["games"])
    for p in players:
        await api(p, "rsvp", game_id=gid, status="yes")
    async with webhook._sessionmaker() as s:
        (await s.get(Game, gid)).starts_at = config.now() + timedelta(minutes=minutes_from_now)
        await s.commit()
    return gid


async def test_notice_late_excuse_and_undo(team):  # noqa: F811
    fake = team  # noqa: F811
    rustam, chira = tg_user(2, "Рустам"), tg_user(3, "Чира")
    rid = await _member(rustam)
    cid = await _member(chira)
    gid = await _game_starting(60, rustam)  # через час

    # «Опаздываю»: только с «Буду», админам — сообщение с причиной
    status, _ = await api(chira, "late_notice", game_id=gid, minutes=15)
    assert status == 400
    status, res = await api(rustam, "late_notice", game_id=gid, minutes=15, reason="пробки")
    assert status == 200 and "15 мин" in res["note"]
    assert any("Рустам опаздывает примерно на 15 мин" in m.text and "пробки" in m.text for m in fake.sent(1))
    g = next(x for x in res["state"]["games"] if x["id"] == gid)
    assert g["my_late"]["late_notice"] == {"minutes": 15, "reason": "пробки"}
    _, st = await api(chira, "state")  # другие видят, что опаздывает, но не причину
    me = next(u for u in next(x for x in st["state"]["games"] if x["id"] == gid)["participants"]["yes"] if u["id"] == rid)
    assert me["late_notice"] == {"minutes": 15, "reason": None}

    # игра идёт; игрок не может сам себе отметить опоздание
    async with webhook._sessionmaker() as s:
        (await s.get(Game, gid)).starts_at = config.now() - timedelta(minutes=30)
        await s.commit()
    assert (await api(rustam, "late", game_id=gid, user_id=rid, minutes=0))[0] == 403
    assert (await api(ADMIN, "late", game_id=gid, user_id=cid, minutes=20))[0] == 400  # Чира не пришёл

    # опоздал на 25 мин → −1 (один минус, сколько бы ни опоздал) и сообщение игроку
    status, res = await api(ADMIN, "late", game_id=gid, user_id=rid, minutes=25)
    assert status == 200 and "−1" in res["note"]
    assert await _open_points(rid) == 1
    assert any("опоздал на 25 мин" in m.text and "−1" in m.text and "форс-мажор" in m.text for m in fake.sent(2))
    g = next(x for x in res["state"]["games"] if x["id"] == gid)
    assert next(u for u in g["participants"]["yes"] if u["id"] == rid)["late"] == {"minutes": 25, "excused": False, "points": 1}

    # поправили на 12 мин — всё ещё −1, не −2: прошлый минус заменяется, а не добавляется; 7 мин — без минуса
    await api(ADMIN, "late", game_id=gid, user_id=rid, minutes=12)
    assert await _open_points(rid) == 1
    await api(ADMIN, "late", game_id=gid, user_id=rid, minutes=7)
    assert await _open_points(rid) == 0
    await api(ADMIN, "late", game_id=gid, user_id=rid, minutes=12)
    # форс-мажор → без минуса
    status, res = await api(ADMIN, "late", game_id=gid, user_id=rid, minutes=12, excused=True)
    assert await _open_points(rid) == 0 and "без минуса" in res["note"]
    assert any("засчитано без минуса" in m.text for m in fake.sent(2))
    # снова без форс-мажора, потом сняли совсем
    await api(ADMIN, "late", game_id=gid, user_id=rid, minutes=30)
    assert await _open_points(rid) == 1
    await api(ADMIN, "late", game_id=gid, user_id=rid, minutes=0)
    assert await _open_points(rid) == 0
    async with webhook._sessionmaker() as s:
        p = await s.get(GameParticipant, (gid, rid))
        assert p.late_minutes is None and p.late_excused is False
        rows = (await s.scalars(select(Penalty).where(
            Penalty.user_id == rid, Penalty.reason == PenaltyReason.LATE))).all()
        assert len(rows) == 1 and rows[0].status == PenaltyStatus.CANCELLED  # одна запись на игру
    assert texts.penalty_reason(PenaltyReason.LATE) == "опоздал"


async def test_late_in_attendance_report(team):  # noqa: F811
    rustam = tg_user(2, "Рустам")
    rid = await _member(rustam)
    for _ in range(2):
        gid = await _game_starting(-60, rustam)
        await api(ADMIN, "late", game_id=gid, user_id=rid, minutes=15)
    gid = await _game_starting(-60, rustam)
    await api(ADMIN, "late", game_id=gid, user_id=rid, minutes=40, excused=True)  # форс-мажор не считается
    async with webhook._sessionmaker() as s:
        for g in (await s.scalars(select(Game))).all():
            g.starts_at = config.now() - timedelta(hours=3)
        await s.commit()
    _, rep = await api(ADMIN, "attendance_report", period="all")
    r = next(x for x in rep["rows"] if x["id"] == rid)
    assert (r["late"], r["late_minutes"], r["came"]) == (2, 30, 3)
    assert r["history"].count("⏰") == 2
    assert "опаздывал 2 раза (всего 30 мин)" in r["attention"]


async def test_admin_cannot_remove_own_minuses(team):  # noqa: F811
    marat = tg_user(5, "Марат")
    mid = await _member(marat)
    await api(ADMIN, "player", user_id=mid, op="admin_on")  # второй админ
    _, st = await api(ADMIN, "state")
    me = st["state"]["user"]["id"]
    gid = await _game_starting(-30, ADMIN, marat)

    # себе опоздание поставить можно, снять/форс-мажор — нет
    assert (await api(ADMIN, "late", game_id=gid, user_id=me, minutes=15))[0] == 200
    assert await _open_points(me) == 1
    status, res = await api(ADMIN, "late", game_id=gid, user_id=me, minutes=15, excused=True)
    assert status == 403 and "другого админа" in res["error"]
    assert (await api(ADMIN, "late", game_id=gid, user_id=me, minutes=0))[0] == 403
    assert await _open_points(me) == 1

    # «Снять» в списке минусов — себе нельзя, другому админу — можно
    _, p = await api(ADMIN, "player_detail", user_id=me)
    pid = p["minuses"][0]["id"]
    assert (await api(ADMIN, "penalty_cancel", penalty_id=pid))[0] == 403
    assert await _open_points(me) == 1
    assert (await api(marat, "penalty_cancel", penalty_id=pid))[0] == 200
    assert await _open_points(me) == 0

    # «не пришёл» → «пришёл» себе (снимает минус за неявку) — нельзя
    await api(ADMIN, "attendance", game_id=gid, user_id=me, present=False)
    assert await _open_points(me) >= 1
    assert (await api(ADMIN, "attendance", game_id=gid, user_id=me, present=True))[0] == 403
    assert (await api(marat, "attendance", game_id=gid, user_id=me, present=True))[0] == 200
