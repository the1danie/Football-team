"""Обязанности «до» (вода) и «после» тренировки (мячи, манишки — с ними и стирка)."""

from datetime import timedelta

import pytest
from sqlalchemy import select, text

from bot import webhook
from bot.config import config
from bot.db import init_db, make_engine, make_sessionmaker
from bot.models import Duty, DutyPhase, Game, GameStatus, Rsvp
from bot.scheduler import tick
from bot.services import games as svc
from tests.conftest import db_url, make_user
from tests.test_miniapp import ADMIN, api, team  # noqa: F401
from tests.test_webhook import fake  # noqa: F401

pytestmark = pytest.mark.real_phases


async def test_default_phases(session):
    duties = {d.code: d for d in await svc.active_duties(session)}
    assert duties["water"].phase == "before"
    assert {duties[c].phase for c in ("balls", "bibs")} == {"after"}
    assert "на следующую" in duties["balls"].action
    assert "постирать" in duties["bibs"].action
    assert "laundry" not in duties  # стирка — часть «Манишек», отдельно выключена


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
    assert {d.code for d in await svc.pending_after_duties(session, game)} == {"balls", "bibs"}
    water_holder = before.assignments[0].user_id

    after = await svc.distribute_game(session, game, rng, phase="after")
    codes = {a.duty.code: a.user_id for a in after.assignments}
    assert set(codes) == {"balls", "bibs"} and game.after_duties_done
    assert codes["balls"] == driver.id  # мячи — на машине
    assert absent.id not in codes.values()  # только среди тех, кто был
    assert water_holder not in codes.values()  # у кого вода — тому не надо (людей хватает)
    assert len(await svc.active_assignments(session, game.id)) == 3  # «до» не сбросилось
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
    assert "Вода —" in draft and "После тренировки (около" in draft and "Мячи" in draft

    # тренировка началась 1,5 часа назад
    async with sm() as s:
        (await s.get(Game, gid)).starts_at = config.now() - timedelta(minutes=61)  # прошёл час
        await s.commit()
    fake.reset()
    async with sm() as s:
        await tick(webhook.make_bot(), s)
        await s.commit()
        codes = {a.duty.code for a in await svc.active_assignments(s, gid)}
        assert codes == {"water", "balls", "bibs"}
    assert any("После тренировки" in m.text and "на тебе" in m.text for m in fake.sent())
    assert any("*После тренировки" in m.text and m.parse_mode is None for m in fake.sent(1))

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
    _, st = await api(ADMIN, "state")
    assert st["state"]["after_minutes"] == 60
    status, _ = await api({"id": 55, "first_name": "Игрок"}, "duties")
    assert status == 403


def test_gather_time(monkeypatch):
    from datetime import datetime

    from bot import texts, whatsapp

    game = Game(kind="training", starts_at=datetime(2026, 10, 5, 23, 0), location="Жас Оркен", status="open")
    now = datetime(2026, 10, 5, 12, 0)
    assert texts.gather_time(game) == "22:30"
    assert "23:00 (сбор в 22:30)" in "\n".join(texts.game_lines(game))  # опрос и напоминания в личку
    assert "Сегодня тренировка в 23:00, сбор в 22:30." in texts.personal_reminder(game, now, [])
    assert "Сбор в 22:30" in whatsapp.clean(whatsapp.announce(game, now, now, "https://t.me/x"))
    assert "*Сегодня тренировка в 23:00, сбор в 22:30*" in whatsapp.clean(whatsapp.reminder(game, now, []))
    monkeypatch.setattr(config, "gather_minutes", 0)
    assert texts.gather_time(game) is None and "сбор" not in texts.personal_reminder(game, now, [])


async def test_gather_time_in_app(team):  # noqa: F811
    tomorrow = config.now() + timedelta(days=1)
    _, res = await api(ADMIN, "create_game", date=tomorrow.date().isoformat(), minutes=23 * 60, kind="training")
    assert res["state"]["games"][0]["gather_time"] == "22:30"


async def test_laundry_merged_into_bibs_on_existing_db(tmp_path, monkeypatch):
    from bot import db
    from bot.models import Assignment, AssignmentStatus

    url = await db_url(tmp_path, "merge.db")
    engine = make_engine(url)
    # «старая» база: стирка — отдельная активная обязанность, без флага объединения
    monkeypatch.setattr(db, "MERGE_LAUNDRY_INTO_BIBS", False)
    monkeypatch.setattr(db, "DEFAULT_DUTIES", [d[:7] + (True,) for d in db.DEFAULT_DUTIES])
    await init_db(engine)
    sm = make_sessionmaker(engine)
    async with sm() as s:
        users = [await make_user(s, n) for n in ("А", "Б", "В")]
        duties = {d.code: d for d in await svc.active_duties(s)}
        old = await svc.create_game(s, "training", config.now() - timedelta(days=7), None, None)
        old.status = "finished"
        s.add(Assignment(game_id=old.id, user=users[0], duty=duties["laundry"]))
        s.add(Assignment(game_id=old.id, user=users[1], duty=duties["bibs"]))
        cur = await svc.create_game(s, "training", config.now() + timedelta(days=1), None, None)
        cur.status = "distributed"
        s.add(Assignment(game_id=cur.id, user=users[2], duty=duties["laundry"]))
        await s.execute(text("DELETE FROM settings WHERE key = 'merge_laundry_v1'"))
        await s.commit()
        old_id, cur_id = old.id, cur.id

    monkeypatch.setattr(db, "MERGE_LAUNDRY_INTO_BIBS", True)
    await init_db(engine)
    async with sm() as s:
        laundry = await s.scalar(select(Duty).where(Duty.code == "laundry"))
        bibs = await s.scalar(select(Duty).where(Duty.code == "bibs"))
        assert laundry.is_active is False and "постирать" in bibs.action
        old_codes = sorted(a.duty.code for a in await svc.active_assignments(s, old_id))
        assert old_codes == ["bibs", "bibs"]  # история: стирал — значит, «Манишки»
        cur_rows = (await s.scalars(select(Assignment).where(Assignment.game_id == cur_id))).all()
        assert [a.status for a in cur_rows] == [AssignmentStatus.CANCELLED]  # на текущей — без двоих на манишки
        stats = dict((d.code, n) for d, n in await svc.player_stats(s, users[0].id))
        assert stats.get("bibs") == 1 and "laundry" not in stats
        laundry.is_active = True  # админ включил обратно
        await s.commit()
    await init_db(engine)  # повторно не трогаем
    async with sm() as s:
        assert (await s.scalar(select(Duty).where(Duty.code == "laundry"))).is_active is True
    await engine.dispose()


def test_whatsapp_texts_have_no_emoji():
    """WhatsApp по ссылке показывает эмодзи как «�» — в текстах для него только слова."""
    from datetime import datetime
    from urllib.parse import unquote

    from bot import whatsapp
    from bot.models import Assignment, User

    game = Game(kind="training", starts_at=datetime(2026, 10, 5, 23, 0), location="Жас Оркен", status="open",
                min_players=5)
    now = datetime(2026, 10, 5, 12, 0)
    u = User(name="Даниял Абуов", telegram_id=1)
    duty = Duty(emoji="🧴", name="Аптечка", action="принести аптечку", phase="before", requires_car=False)
    texts_ = [
        whatsapp.announce(game, now, now, "https://t.me/x?start=game_1"),
        whatsapp.status(game, {"yes": [u], "maybe": [], "no": []}, "https://t.me/x"),
        whatsapp.nudge(game, [u], now, now, "https://t.me/x", 2),
        whatsapp.duties(game, 3, [Assignment(user=u, duty=duty)], [duty], "https://t.me/x"),
        whatsapp.reminder(game, now, [Assignment(user=u, duty=duty)]),
        whatsapp.after_duties(game, [Assignment(user=u, duty=duty)], []),
        "⚽ ✅ 🤔 ❌ 📍 🕢 ⏰ 🙈 🏁 ⚠️ ✏️ 🔄 👕",
    ]
    for t in texts_:
        cleaned = whatsapp.clean(t)
        assert not whatsapp._EMOJI.search(cleaned), cleaned
        assert not whatsapp._EMOJI.search(unquote(whatsapp.share_url(t).split("text=")[1]))
    assert whatsapp.clean(texts_[0]).startswith("*Тренировка — 5 октября (пн), 23:00*\nСбор в 22:30\nМесто: Жас Оркен")
    assert "Буду (1): Даниял Абуов" in whatsapp.clean(texts_[1])
    assert "Аптечка — Даниял Абуов" in whatsapp.clean(texts_[3])


@pytest.mark.real_phases
async def test_recalculate_reshuffles_all_phases(session):
    """«Пересчитать»: заново и вода, и мячи с манишками — каждому по возможности другая обязанность."""
    import random

    users = []
    for i in range(6):
        u = await svc.get_or_create_user(session, 100 + i, f"P{i}", None)
        u.profile_completed, u.status, u.has_car = True, "approved", True
        users.append(u)
    game = await svc.create_game(session, "training", config.now() + timedelta(hours=1), None, None)
    for u in users:
        await svc.set_rsvp(session, game, u, Rsvp.YES)
    rng = random.Random(1)
    await svc.distribute_game(session, game, rng)
    await svc.distribute_game(session, game, rng, phase=DutyPhase.AFTER)
    before = {a.duty_id: a.user_id for a in await svc.active_assignments(session, game.id)}
    assert len(before) >= 3

    for phase in (DutyPhase.BEFORE, DutyPhase.AFTER):
        await svc.distribute_game(session, game, rng, phase=phase, reshuffle=True)
    after = {a.duty_id: a.user_id for a in await svc.active_assignments(session, game.id)}
    assert after.keys() == before.keys()
    assert all(after[d] != before[d] for d in before), (before, after)
