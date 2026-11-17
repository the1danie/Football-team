"""Два Telegram у одного игрока: привязать по ссылке, админом (потерял доступ), отвязать; сообщения — на оба."""

import time

from bot import notifier, weblink
from bot.config import config
from bot.services import games as svc
from tests.test_bot_flow import ADMIN, Harness, h  # noqa: F401 — фикстура h
from tests.test_miniapp import ADMIN as APP_ADMIN, api, team, tg_user  # noqa: F401
from tests.test_webhook import fake  # noqa: F401


async def test_link_second_telegram_by_own_link(h: Harness, monkeypatch):
    monkeypatch.setattr(config, "bot_token", "42:TEST")  # ссылка подписывается токеном бота
    fake = h.fake
    await h.register(ADMIN, "Даниял", car=True)
    await h.register(2, "Рустам", car=False)
    async with h.sm() as s:
        rus = await svc.get_user_by_tg(s, 2)
        payload = weblink.tg2_payload(rus)
    fake.reset()
    h.names[7] = "Рустам2"
    await h.text(7, f"/start {payload}")
    assert any("привязан к игроку" in m.text for m in fake.sent(7))
    assert any("К твоему профилю привязан" in m.text for m in fake.sent(2))  # старый аккаунт предупреждён
    async with h.sm() as s:
        assert (await svc.get_user_by_tg(s, 7)).id == rus.id
        assert await svc.telegram_ids(s, rus) == [2, 7]
        fake.reset()
        await notifier.send_dm(h.bot, await svc.get_user_by_tg(s, 2), "проверка")
    assert [m.chat_id for m in fake.sent() if m.text == "проверка"] == [2, 7]  # на оба
    # просроченная ссылка не работает
    async with h.sm() as s:
        old = weblink.tg2_payload(rus, now=time.time() - 2 * weblink.TG2_TTL)
    fake.reset()
    await h.text(8, f"/start {old}")
    assert any("устарела" in m.text for m in fake.sent(8))


async def test_admin_links_new_account_and_unlinks_old(team):  # noqa: F811
    rustam = tg_user(2, "Рустам")
    await api(rustam, "register", car=False)
    _, pl = await api(APP_ADMIN, "players")
    rid = next(p["id"] for p in pl["players"] if p["name"] == "Рустам")
    await api(APP_ADMIN, "player", user_id=rid, op="approve")
    # потерял доступ: заходит с нового аккаунта — заявка
    new = tg_user(9, "Рустам Новый")
    await api(new, "register", car=False)
    _, pl = await api(APP_ADMIN, "players")
    pending = next(p["id"] for p in pl["players"] if p["name"] == "Рустам Новый")
    status, res = await api(APP_ADMIN, "player", user_id=pending, op="link_to", target_id=rid)
    assert status == 200 and "ещё один Telegram" in res["note"]
    assert [a["telegram_id"] for a in res["player"]["accounts"]] == [2, 9]
    assert not any(p["id"] == pending for p in res["players"])  # заявка исчезла
    _, st = await api(new, "state")
    assert st["state"]["user"]["id"] == rid and st["state"]["access"] == "ok"
    # старый отвязали — основным стал новый; последний отвязать нельзя
    _, res = await api(APP_ADMIN, "player_tg_remove", user_id=rid, telegram_id=2)
    assert [(a["telegram_id"], a["primary"]) for a in res["player"]["accounts"]] == [(9, True)]
    assert (await api(APP_ADMIN, "player_tg_remove", user_id=rid, telegram_id=9))[0] == 400
    assert (await api(rustam, "state"))[1]["state"]["access"] == "new"  # со старого — больше не он
    assert (await api(new, "player_tg_remove", user_id=rid, telegram_id=9))[0] == 403  # только админ


async def test_admin_second_account_is_admin(team):  # noqa: F811
    _, st = await api(APP_ADMIN, "state")
    me = st["state"]["user"]["id"]
    _, r = await api(APP_ADMIN, "tg2_link")
    assert "start=tg2_" in r["url"]
    from bot import webhook

    async with webhook._sessionmaker() as s:
        await svc.add_telegram(s, await s.get(svc.User, me), 555, "daniyal2")
        await s.commit()
    _, st = await api(tg_user(555, "Даниял2"), "state")
    assert st["state"]["user"]["id"] == me and st["state"]["is_admin"] is True
    assert 555 in config.all_admin_ids
