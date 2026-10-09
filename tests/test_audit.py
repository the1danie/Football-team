"""Журнал действий админов: пишутся действия админов (не бота и не игроков), видит только главный."""

from datetime import timedelta

from bot.config import config
from tests.test_bot_flow import ADMIN, h  # noqa: F401 — фикстура h
from tests.test_miniapp import ADMIN as OWNER
from tests.test_miniapp import api, team, tg_user  # noqa: F401 — фикстура team
from tests.test_webhook import fake  # noqa: F401


async def _member(user):
    await api(user, "register", car=False)
    _, pl = await api(OWNER, "players")
    uid = next(p["id"] for p in pl["players"] if p["name"] == user["first_name"])
    await api(OWNER, "player", user_id=uid, op="approve")
    return uid


async def test_delegated_admin_actions_are_logged(team):  # noqa: F811
    marat, pasha = tg_user(3, "Марат"), tg_user(4, "Паша")
    marat_id = await _member(marat)
    pasha_id = await _member(pasha)
    await api(OWNER, "player", user_id=marat_id, op="admin_on")
    # Марат-админ: создаёт игру, отмечает за Пашу, убирает его из состава, отменяет игру
    tomorrow = (config.now() + timedelta(days=1)).date().isoformat()
    _, res = await api(marat, "create_game", date=tomorrow, minutes=20 * 60, kind="training", location="Жас Оркен")
    gid = res["state"]["games"][0]["id"]
    await api(marat, "rsvp", game_id=gid, status="yes")  # своя отметка — не пишется
    await api(marat, "rsvp_for", game_id=gid, user_id=pasha_id, status="no")
    await api(marat, "player", user_id=pasha_id, op="active")
    await api(marat, "cancel", game_id=gid)
    await api(pasha, "profile", car=True)  # игрок сам — не пишется

    status, log = await api(OWNER, "audit")
    assert status == 200
    texts = [e["text"] for e in log["entries"] if e["actor"] == "Марат"]
    assert any(t.startswith("➕ Создал") and "Тренировка" in t for t in texts)
    assert any("Отметил за Паша: ❌ Не буду" in t for t in texts)
    assert any("Паша: убрал из состава" in t for t in texts)
    assert any(t.startswith("❌ Отменил") for t in texts)
    assert not any("Буду" in t and "за" not in t for t in texts)
    assert all(e["day"] == "Сегодня" for e in log["entries"])
    owner_texts = [e["text"] for e in log["entries"] if e["actor"] == "Даниял"]
    assert any("Марат: выдал права админа" in t for t in owner_texts)
    # фильтр по админу
    _, only = await api(OWNER, "audit", actor_id=marat_id)
    assert {e["actor"] for e in only["entries"]} == {"Марат"}
    assert {a["name"] for a in log["actors"]} == {"Марат", "Даниял"}
    # делегированный админ и игрок журнал не видят
    assert (await api(marat, "audit"))[0] == 403
    assert (await api(pasha, "audit"))[0] == 403


async def test_log_command_and_scheduler_not_logged(h):  # noqa: F811
    from bot.scheduler import tick

    await h.register(ADMIN, "Админ", True)
    await h.register(2, "Паша", False)
    async with h.sm() as s:
        from bot.services import games as svc

        await svc.create_game(s, "training", config.now() + timedelta(hours=2), None, None)
        await s.commit()
    async with h.sm() as s:
        await tick(h.bot, s)  # бот сам распределяет — не админ, в журнал не идёт
        await s.commit()
    await h.text(ADMIN, "/log")
    msg = h.fake.sent(ADMIN)[-1].text
    assert "Паша: принял в команду" in msg and "Распределил" not in msg
    await h.text(2, "/log")
    assert "Журнал" not in (h.fake.sent(2)[-1].text if h.fake.sent(2) else "")


async def test_audit_hidden_in_view_as_preview(team):  # noqa: F811
    _, st = await api(OWNER, "state", view_as="player")
    assert st["state"]["is_owner"] is False and st["state"]["real_owner"] is True
    assert (await api(OWNER, "audit", view_as="player"))[0] == 403
    assert (await api(OWNER, "audit"))[0] == 200


async def test_second_admin_cannot_approve_again(team):  # noqa: F811
    marat = tg_user(3, "Марат")
    marat_id = await _member(marat)
    await api(OWNER, "player", user_id=marat_id, op="admin_on")
    rustam = tg_user(7, "Рустам")
    await api(rustam, "register", car=False)
    _, pl = await api(OWNER, "players")
    rid = next(p["id"] for p in pl["players"] if p["name"] == "Рустам")
    status, _ = await api(marat, "player", user_id=rid, op="approve")
    assert status == 200
    status, body = await api(OWNER, "player", user_id=rid, op="approve")
    assert status == 400 and body["error"] == "Рустам уже в команде — принял Марат."
    _, log = await api(OWNER, "audit")
    assert sum("Рустам: принял в команду" in e["text"] for e in log["entries"]) == 1


async def test_only_owner_distributes_and_can_undo(team):  # noqa: F811
    fake = team  # noqa: F811
    from bot import webhook
    from bot.services import games as svc

    marat, pasha = tg_user(3, "Марат"), tg_user(4, "Паша")
    marat_id = await _member(marat)
    await _member(pasha)
    await api(OWNER, "player", user_id=marat_id, op="admin_on")
    tomorrow = (config.now() + timedelta(days=2)).date().isoformat()
    _, res = await api(OWNER, "create_game", date=tomorrow, minutes=21 * 60 + 30, kind="training")
    gid = res["state"]["games"][0]["id"]
    for u in (OWNER, marat, pasha):
        await api(u, "rsvp", game_id=gid, status="yes")
    status, body = await api(marat, "distribute", game_id=gid)
    assert status == 403 and "только главный админ" in body["error"]
    _, res = await api(OWNER, "distribute", game_id=gid)  # рано, но главный может
    assert res["state"]["games"][0]["status"] == "distributed"
    assert (await api(marat, "undistribute", game_id=gid))[0] == 403
    _, res = await api(OWNER, "undistribute", game_id=gid)
    g = res["state"]["games"][0]
    assert g["status"] == "open" and g["duties"] == [] and "Распределение отменено" in res["note"]
    assert any("Распределение на" in m.text and "отменено" in m.text for m in fake.sent(4) + fake.sent(3))
    async with webhook._sessionmaker() as s:
        assert await svc.active_assignments(s, gid) == []
    _, log = await api(OWNER, "audit")
    assert any(e["text"].startswith("↩️ Отменил распределение") for e in log["entries"])


async def test_fix_duty_in_past_game(team):  # noqa: F811
    """Мячи брал другой игрок, а в боте не исправили — исправляем в архиве задним числом."""
    from datetime import datetime

    from sqlalchemy import select

    from bot import webhook
    from bot.models import Game, GameStatus, Penalty, PenaltyStatus
    from bot.services import games as svc

    fake = team  # noqa: F811
    marat, pasha = tg_user(3, "Марат"), tg_user(4, "Паша")
    await _member(marat)
    pasha_id = await _member(pasha)
    day = (config.now() + timedelta(days=1)).date().isoformat()
    _, res = await api(OWNER, "create_game", date=day, minutes=20 * 60, kind="training")
    gid = res["state"]["games"][0]["id"]
    for u in (OWNER, marat):
        await api(u, "rsvp", game_id=gid, status="yes")
    await api(OWNER, "distribute", game_id=gid)
    async with webhook._sessionmaker() as s:
        game = await s.get(Game, gid)
        balls = next(a for a in await svc.active_assignments(s, gid) if a.duty.name == "Мячи")
        balls_duty_id, first = balls.duty_id, balls.user
        # у обоих был минус; при завершении игры минус списался тому, кто «вёз мячи» по боту
        for uid in (first.id, pasha_id):
            s.add(Penalty(user_id=uid, game_id=None, points=1, created_at=datetime.utcnow() - timedelta(days=3)))
        await s.flush()
        await svc.redeem_penalties(s, game)
        game.starts_at = config.now() - timedelta(days=1)
        game.status = GameStatus.FINISHED
        first_id = first.id
        await s.commit()
    sent_before = len(fake.sent(4))

    _, arch = await api(OWNER, "archive")
    g = next(x for x in arch["archive"] if x["id"] == gid)
    assert g["past"] and next(d for d in g["duties"] if d["duty_id"] == balls_duty_id)["user"]["id"] == first_id
    status, res = await api(OWNER, "assign", game_id=gid, duty_id=balls_duty_id, user_id=pasha_id)
    assert status == 200, res

    async with webhook._sessionmaker() as s:
        now_balls = next(a for a in await svc.active_assignments(s, gid) if a.duty_id == balls_duty_id)
        assert now_balls.user_id == pasha_id
        assert (await svc.attendance(s, gid))[pasha_id] is True  # отмечен «пришёл»
        pens = {p.user_id: p for p in (await s.scalars(select(Penalty))).all()}
        assert pens[pasha_id].status == PenaltyStatus.REDEEMED and pens[pasha_id].redeemed_game_id == gid
        assert pens[first_id].status == PenaltyStatus.OPEN
        board = {r["id"]: r for r in await svc.leaderboard(s, config.now())}
        assert board[pasha_id]["duties"] == 1
    assert len(fake.sent(4)) == sent_before  # задним числом — без сообщений
    _, log = await api(OWNER, "audit")
    assert any("Исправил задним числом" in e["text"] and "→ Паша" in e["text"] for e in log["entries"])


async def test_fix_past_duty_with_player_who_joined_later(team):  # noqa: F811
    """Дидара не было в боте во время игры — его всё равно можно выбрать в «Кто что делал»."""
    from datetime import datetime

    from bot import webhook
    from bot.models import Game, GameStatus
    from bot.services import games as svc

    day = (config.now() + timedelta(days=1)).date().isoformat()
    _, res = await api(OWNER, "create_game", date=day, minutes=20 * 60, kind="training")
    gid = res["state"]["games"][0]["id"]
    await api(OWNER, "rsvp", game_id=gid, status="yes")
    await api(OWNER, "distribute", game_id=gid)
    async with webhook._sessionmaker() as s:
        game = await s.get(Game, gid)
        game.starts_at = config.now() - timedelta(days=7)
        game.created_at = datetime.utcnow() - timedelta(days=9)
        game.status = GameStatus.FINISHED
        await s.commit()
    didar_id = await _member(tg_user(8, "Дидар"))  # пришёл в бота уже после игры
    _, arch = await api(OWNER, "archive")
    g = next(x for x in arch["archive"] if x["id"] == gid)
    assert didar_id in {u["id"] for u in g["team"]}
    assert didar_id not in {u["id"] for u in g["no_answer"]}
    balls = next(d for d in g["duties"] if d["name"] == "Мячи")
    status, _ = await api(OWNER, "assign", game_id=gid, duty_id=balls["duty_id"], user_id=didar_id)
    assert status == 200
    async with webhook._sessionmaker() as s:
        assert next(a for a in await svc.active_assignments(s, gid) if a.duty_id == balls["duty_id"]).user_id == didar_id
    _, me = await api(tg_user(8, "Дидар"), "archive")
    assert "team" not in next(x for x in me["archive"] if x["id"] == gid)  # игрокам список команды не нужен
