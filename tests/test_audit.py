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
