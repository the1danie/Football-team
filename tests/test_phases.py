"""Обязанности «до» (вода) и «после» тренировки (мячи, манишки, стирка)."""

from datetime import timedelta

import pytest
from sqlalchemy import select, text

from bot import webhook
from bot.config import config
from bot.db import init_db, make_engine, make_sessionmaker
from bot.models import Duty, Game, GameStatus
from bot.scheduler import tick
from bot.services import games as svc
from tests.conftest import db_url, make_user
from tests.test_miniapp import ADMIN, api, team  # noqa: F401
from tests.test_webhook import fake  # noqa: F401

pytestmark = pytest.mark.real_phases


async def test_default_phases(session):
    duties = {d.code: d for d in await svc.active_duties(session)}
    assert duties["water"].phase == "before"
    assert {duties[c].phase for c in ("balls", "bibs", "laundry")} == {"after"}
    assert "на следующую" in duties["balls"].action


async def test_before_then_after(session, rng):
    driver = await make_user(session, "Водитель", has_car=True)
    others = [await make_user(session, n) for n in ("А", "Б", "В")]
    absent = await make_user(session, "Не пришёл", has_car=True)
    game = await make_game_now(session)
    for u in [driver, *others]:
        await svc.set_rsvp(session, game, u, "yes")
    await svc.set_rsvp(session, game, absent, "no")

    before = await svc.distribute_game(session, game, rng)
    assert [a.duty.code for a in before.assignments] == ["water"]
    assert game.status == GameStatus.DISTRIBUTED and not game.after_duties_done
    assert await svc.unassigned_duties(session, game) == []  # «после» ещё не наступило
    assert {d.code for d in await svc.pending_after_duties(session, game)} == {"balls", "bibs", "laundry"}
    water_holder = before.assignments[0].user_id

    after = await svc.distribute_game(session, game, rng, phase="after")
    codes = {a.duty.code: a.user_id for a in after.assignments}
    assert set(codes) == {"balls", "bibs", "laundry"} and game.after_duties_done
    assert codes["balls"] == driver.id  # мячи — на машине
    assert absent.id not in codes.values()  # только среди тех, кто был
    assert water_holder not in codes.values()  # у кого вода — тому не надо (людей хватает)
    assert len(await svc.active_assignments(session, game.id)) == 4  # «до» не сбросилось
    assert await svc.pending_after_duties(session, game) == []


async def make_game_now(session):
    return await svc.create_game(session, "training", config.now() + timedelta(hours=1), None, None)


async def test_scheduler_after_training(fake, monkeypatch):  # noqa: F811
    monkeypatch.setattr(config, "admin_ids", [1])
    sm, _ = await webhook._ensure_ready()
    async with sm() as s:
        users = [await make_user(s, n, has_car=(n == "Даниял")) for n in ("Даниял", "Арман", "Тимур", "Руслан")]
        game = await svc.create_game(s, "training", config.now() + timedelta(days=1), "Жас Оркен", None)
        for u in users:
            await svc.set_rsvp(s, game, u, "yes")
        game.created_at -= timedelta(days=2)
        game.starts_at = config.now() + timedelta(hours=4)  # сбор закрыт
        await s.commit()
        gid = game.id

    fake.reset()
    async with sm() as s:
        await tick(webhook.make_bot(), s)
        await s.commit()
        assert {a.duty.code for a in await svc.active_assignments(s, gid)} == {"water"}
    draft = next(m.text for m in fake.sent(1) if m.parse_mode is None and "*Обязанности*" in m.text)
    assert "💧 Вода —" in draft and "После тренировки (около" in draft and "⚽ Мячи" in draft

    # тренировка началась 1,5 часа назад
    async with sm() as s:
        (await s.get(Game, gid)).starts_at = config.now() - timedelta(minutes=91)
        await s.commit()
    fake.reset()
    async with sm() as s:
        await tick(webhook.make_bot(), s)
        await s.commit()
        codes = {a.duty.code for a in await svc.active_assignments(s, gid)}
        assert codes == {"water", "balls", "bibs", "laundry"}
    assert any("После тренировки" in m.text and "на тебе" in m.text for m in fake.sent())
    assert any("🏁 После тренировки" in m.text and m.parse_mode is None for m in fake.sent(1))

    fake.reset()
    async with sm() as s:
        await tick(webhook.make_bot(), s)
        await s.commit()
    assert not any("После тренировки" in m.text for m in fake.sent())  # не повторяем


async def test_existing_db_gets_phases_once(tmp_path):
    url = await db_url(tmp_path, "phases.db")
    engine = make_engine(url)
    await init_db(engine)
    async with engine.begin() as conn:  # «старая» база: всё «до», старые формулировки, нет флага
        await conn.execute(text("UPDATE duties SET phase = 'before', action = 'привезти мячи' WHERE code = 'balls'"))
        await conn.execute(text("DELETE FROM settings WHERE key = 'duty_phases_v1'"))
    await init_db(engine)
    sm = make_sessionmaker(engine)
    async with sm() as s:
        balls = await s.scalar(select(Duty).where(Duty.code == "balls"))
        assert balls.phase == "after" and "на следующую" in balls.action
        balls.phase = "before"  # админ решил по-своему
        await s.commit()
    await init_db(engine)  # повторный старт не перезатирает
    async with sm() as s:
        assert (await s.scalar(select(Duty).where(Duty.code == "balls"))).phase == "before"
    await engine.dispose()


async def test_miniapp_duty_settings(team):  # noqa: F811
    _, res = await api(ADMIN, "duties")
    water = next(d for d in res["duties"] if d["name"] == "Вода")
    assert water["phase"] == "before"
    _, res = await api(ADMIN, "duty_update", id=water["id"], phase="after", requires_car=True)
    water = next(d for d in res["duties"] if d["name"] == "Вода")
    assert water["phase"] == "after" and water["requires_car"] is True
    status, _ = await api({"id": 55, "first_name": "Игрок"}, "duties")
    assert status == 403
