"""Игроки без Telegram (добавлены вручную), архив прошедших игр, аналитика посещаемости."""

from datetime import datetime, timedelta

from bot import webhook
from bot.config import config
from bot.models import Game, GameStatus, Rsvp
from bot.services import games as svc
from bot.weblink import link_payload, parse_link_payload
from tests.test_bot_flow import ADMIN, h  # noqa: F401 — фикстура h
from tests.test_miniapp import api, team, tg_user  # noqa: F401 — фикстура team
from tests.test_miniapp import ADMIN as ADMIN_TG
from tests.test_webhook import fake  # noqa: F401


async def test_manual_player_penalized_marked_and_linked_by_invite(h):  # noqa: F811
    await h.register(ADMIN, "Админ", True)
    async with h.sm() as s:
        nurik = await svc.create_manual_user(s, "Нурик", False)
        game = await svc.create_game(s, "training", config.now() + timedelta(hours=1), "Жас Оркен", None)
        game.created_at = datetime.utcnow() - timedelta(days=2)
        nurik.created_at = datetime.utcnow() - timedelta(days=3)  # добавлен до игры
        await s.commit()
        nurik_id, game_id = nurik.id, game.id
        assert nurik.is_manual and nurik.telegram_id < 0
        assert nurik_id in {u.id for u in await svc.non_responders(s, game)}
        res = await svc.apply_no_response_penalties(s, game)
        assert [r.user.name for r in res] == ["Нурик"]  # молчит — минус, как всем
        await s.commit()
        payload = link_payload(nurik)
    assert parse_link_payload(payload) == nurik_id
    assert parse_link_payload(payload[:-1] + "x") is None

    # он заходит по приглашению с настоящего Telegram — профиль привязывается, минус сохраняется
    h.names[555] = "Nurlan T"
    await h.text(555, f"/start {payload}")
    assert any("Ты в команде" in m.text for m in h.fake.sent(555))
    assert any("привязан" in m.text for m in h.fake.sent(ADMIN))
    async with h.sm() as s:
        user = await svc.get_user_by_tg(s, 555)
        assert user.id == nurik_id and user.name == "Нурик" and not user.is_manual
        assert (await svc.open_penalty_points(s, [nurik_id]))[nurik_id] == config.penalty_points
        await svc.set_rsvp(s, await svc.get_game(s, game_id), user, Rsvp.YES)
    # второй раз ссылка уже не работает для другого человека
    await h.text(777, f"/start {payload}")
    assert any("уже зашёл другой человек" in m.text for m in h.fake.sent(777))


async def test_manual_player_via_app(team):  # noqa: F811
    fake = team  # noqa: F811
    status, res = await api(ADMIN_TG, "player_add", name="Нурик", car=False)
    assert status == 200 and res["player"]["manual"] is True
    nurik_id = res["player"]["id"]
    status, _ = await api(ADMIN_TG, "player_add", name="нурик")
    assert status == 400  # дубль имени
    tomorrow = (config.now() + timedelta(days=1)).date().isoformat()
    _, res = await api(ADMIN_TG, "create_game", date=tomorrow, minutes=20 * 60, kind="training")
    assert "Без Telegram (добавлены вручную): 1" in res["note"]
    g = res["state"]["games"][0]
    assert any(u["id"] == nurik_id and u["manual"] for u in g["no_answer"])
    # админ отмечает за него (ответил в WhatsApp)
    _, res = await api(ADMIN_TG, "rsvp_for", game_id=g["id"], user_id=nurik_id, status="yes")
    assert [u["name"] for u in res["state"]["games"][0]["participants"]["yes"]] == ["Нурик"]
    status, _ = await api(tg_user(9, "Чужой"), "rsvp_for", game_id=g["id"], user_id=nurik_id, status="no")
    assert status in (401, 403)
    # приглашение: ссылка в бота с кодом
    _, inv = await api(ADMIN_TG, "player_invite", user_id=nurik_id)
    assert "?start=link_" in inv["tg_link"] and "Нурик" in inv["text"] and inv["url"].startswith("https://wa.me/")

    # живой человек подал заявку — админ связывает её с Нуриком
    real = tg_user(31, "Nurlan")
    await api(real, "register", car=True)
    _, pl = await api(ADMIN_TG, "players")
    pending_id = next(p["id"] for p in pl["players"] if p["status"] == "pending")
    status, res = await api(ADMIN_TG, "player", user_id=pending_id, op="link_to", target_id=nurik_id)
    assert status == 200 and res["player"]["manual"] is False
    assert not any(p["id"] == pending_id for p in res["players"])
    _, st = await api(real, "state")
    assert st["state"]["access"] == "ok" and st["state"]["user"]["id"] == nurik_id
    assert st["state"]["games"][0]["my_rsvp"] == "yes"  # отметка, поставленная админом, сохранилась
    assert any("Ты в команде" in m.text for m in fake.sent(31))


async def test_archive_and_attendance_report(team):  # noqa: F811
    arman = tg_user(2, "Арман")
    await api(arman, "register", car=False)
    _, pl = await api(ADMIN_TG, "players")
    arman_id = next(p["id"] for p in pl["players"] if p["name"] == "Арман")
    await api(ADMIN_TG, "player", user_id=arman_id, op="approve")
    tomorrow = (config.now() + timedelta(days=1)).date().isoformat()
    ids = []
    for days in (2, 3):
        d = (config.now() + timedelta(days=days)).date().isoformat()
        _, res = await api(ADMIN_TG, "create_game", date=d, minutes=20 * 60, kind="training")
        ids.append(max(g["id"] for g in res["state"]["games"]))
    await api(ADMIN_TG, "rsvp", game_id=ids[0], status="yes")
    await api(arman, "rsvp", game_id=ids[0], status="yes")
    await api(ADMIN_TG, "rsvp", game_id=ids[1], status="yes")  # Арман молчит на второй
    async with webhook._sessionmaker() as s:
        users = {u.name: u for u in await svc.all_users(s)}
        for i, gid in enumerate(ids):
            game = await s.get(Game, gid)
            game.created_at = datetime.utcnow() - timedelta(days=10)
            game.starts_at = config.now() - timedelta(days=2 - i, hours=3)
            game.status = GameStatus.FINISHED
        await s.flush()
        users["Арман"].created_at = datetime.utcnow() - timedelta(days=30)
        await svc.set_attendance(s, await s.get(Game, ids[0]), users["Арман"], False)  # неявка
        await s.commit()
    _, res = await api(ADMIN_TG, "create_game", date=tomorrow, minutes=20 * 60, kind="game")
    assert [g["id"] for g in res["state"]["games"] if g["id"] in ids] == []  # прошедшие ушли из «Игр»
    _, arch = await api(arman, "archive")
    assert [g["id"] for g in arch["archive"]] == [ids[1], ids[0]] and all(g["past"] for g in arch["archive"])

    _, rep = await api(ADMIN_TG, "attendance_report", period="all")
    arman_row = next(r for r in rep["rows"] if r["name"] == "Арман")
    assert arman_row["games"] == 2 and arman_row["came"] == 0 and arman_row["no_show"] == 1 and arman_row["silent"] == 1
    assert arman_row["rate"] == 0 and arman_row["history"] == "🚫🔇"
    assert rep["rows"][0]["name"] == "Арман"  # худшие сверху
    status, _ = await api(arman, "attendance_report")
    assert status == 403


async def test_rsvp_locked_after_deadline_goes_through_admin(h):  # noqa: F811
    from tests.test_bot_flow import buttons

    await h.register(ADMIN, "Админ", True)
    await h.register(2, "Паша", False)
    async with h.sm() as s:
        game = await svc.create_game(s, "training", config.now() + timedelta(hours=2), "Жас Оркен", None)
        game.created_at = datetime.utcnow() - timedelta(days=2)
        pasha = await svc.get_user_by_tg(s, 2)
        await svc.set_rsvp(s, game, pasha, Rsvp.YES)
        await s.commit()
        game_id, pasha_id = game.id, pasha.id
    # сбор закрыт (за 5 ч до начала) — сам поменять нельзя, админу уходит запрос
    await h.click(2, f"rsvp:{game_id}:no")
    async with h.sm() as s:
        assert await svc.get_rsvp(s, game_id, pasha_id) == Rsvp.YES
    req = [m for m in h.fake.sent(ADMIN) if "просит изменить ответ" in m.text]
    assert req and "Не буду" in req[-1].text
    ok = next(b for b in buttons(req[-1].reply_markup) if b.text.startswith("✅"))
    await h.click(ADMIN, ok.callback_data)
    async with h.sm() as s:
        assert await svc.get_rsvp(s, game_id, pasha_id) == Rsvp.NO
    assert any("Админ изменил твой ответ" in m.text for m in h.fake.sent(2))
    # админ сам по-прежнему может менять свой ответ
    await h.click(ADMIN, f"rsvp:{game_id}:yes")
    async with h.sm() as s:
        assert await svc.get_rsvp(s, game_id, (await svc.get_user_by_tg(s, ADMIN)).id) == Rsvp.YES


async def test_rsvp_locked_in_app(team):  # noqa: F811
    fake = team  # noqa: F811
    pasha = tg_user(2, "Паша")
    await api(pasha, "register", car=False)
    _, pl = await api(ADMIN_TG, "players")
    await api(ADMIN_TG, "player", user_id=next(p["id"] for p in pl["players"] if p["name"] == "Паша"), op="approve")
    tomorrow = (config.now() + timedelta(days=1)).date().isoformat()
    _, res = await api(ADMIN_TG, "create_game", date=tomorrow, minutes=20 * 60, kind="training")
    gid = res["state"]["games"][0]["id"]
    await api(pasha, "rsvp", game_id=gid, status="yes")
    async with webhook._sessionmaker() as s:
        game = await s.get(Game, gid)
        game.created_at = datetime.utcnow() - timedelta(days=2)
        game.starts_at = config.now() + timedelta(hours=2)
        await s.commit()
    status, res = await api(pasha, "rsvp", game_id=gid, status="no")
    assert status == 200 and "Сбор закрыт" in res["note"] and "отправлен админу" in res["note"]
    assert res["state"]["games"][0]["my_rsvp"] == "yes"
    assert any("просит изменить ответ" in m.text for m in fake.sent(1))


async def test_delete_test_game_from_archive(team):  # noqa: F811
    tomorrow = (config.now() + timedelta(days=1)).date().isoformat()
    _, res = await api(ADMIN_TG, "create_game", date=tomorrow, minutes=20 * 60, kind="training")
    gid = res["state"]["games"][0]["id"]
    status, body = await api(ADMIN_TG, "delete_game", game_id=gid)
    assert status == 400 and "сначала отмените" in body["error"]
    await api(ADMIN_TG, "rsvp", game_id=gid, status="yes")
    await api(ADMIN_TG, "cancel", game_id=gid)
    _, arch = await api(ADMIN_TG, "archive")
    assert [g["id"] for g in arch["archive"]] == [gid]
    status, _ = await api(tg_user(9, "Чужой"), "delete_game", game_id=gid)
    assert status in (401, 403)
    _, res = await api(ADMIN_TG, "delete_game", game_id=gid)
    assert res["note"] == "🗑 Игра удалена"
    _, arch = await api(ADMIN_TG, "archive")
    assert arch["archive"] == []
    async with webhook._sessionmaker() as s:
        assert (await s.get(Game, gid)).status == GameStatus.DELETED  # запись осталась — расписание не создаст её снова
