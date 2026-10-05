"""Штаб команды (тренер, директор): не отмечается, без опросов и минусов, но видит состав."""

from datetime import timedelta

from bot import webhook
from bot.config import config
from bot.services import games as svc
from tests.test_miniapp import ADMIN, api, team, tg_user  # noqa: F401 — фикстура team
from tests.test_webhook import fake  # noqa: F401

COACH = tg_user(5, "Серик")
ARMAN = tg_user(2, "Арман")


async def _join(user):
    await api(user, "register", car=False)
    _, pl = await api(ADMIN, "players")
    uid = next(p["id"] for p in pl["players"] if p["name"] == user["first_name"])
    await api(ADMIN, "player", user_id=uid, op="approve")
    return uid


async def test_coach_sees_but_does_not_play(team):  # noqa: F811
    fake = team  # noqa: F811
    coach_id = await _join(COACH)
    await _join(ARMAN)
    tomorrow = (config.now() + timedelta(days=1)).date().isoformat()
    _, res = await api(ADMIN, "create_game", date=tomorrow, minutes=20 * 60, kind="training", location="Динамо")
    gid = res["state"]["games"][0]["id"]
    await api(COACH, "rsvp", game_id=gid, status="yes")  # пока игрок — отметился

    # админ делает тренером: отметка снимается, игрок уведомлён
    status, res = await api(ADMIN, "player", user_id=coach_id, op="staff", name="Тренер")
    assert status == 200 and res["player"]["staff"] == "Тренер"
    assert any("штабе команды" in m.text for m in fake.sent(5))
    async with webhook._sessionmaker() as s:
        assert await svc.get_rsvp(s, gid, coach_id) is None
        assert coach_id not in {u.id for u in await svc.roster(s)}

    # тренер не может отметиться, но видит всех, включая молчащих
    status, body = await api(COACH, "rsvp", game_id=gid, status="yes")
    assert status == 400 and "отмечаться не нужно" in body["error"]
    await api(ARMAN, "rsvp", game_id=gid, status="yes")
    _, st = await api(COACH, "state")
    g = st["state"]["games"][0]
    assert st["state"]["user"]["staff"] == "Тренер"
    assert [u["name"] for u in g["participants"]["yes"]] == ["Арман"]
    assert {u["name"] for u in g["no_answer"]} == {"Даниял"}  # тренера среди молчащих нет

    # новая игра: тренеру — уведомление без кнопок ответа
    day2 = (config.now() + timedelta(days=2)).date().isoformat()
    await api(ADMIN, "create_game", date=day2, minutes=20 * 60, kind="game")
    note = [m for m in fake.sent(5) if "тебе отмечаться не нужно" in m.text]
    assert note and not any("rsvp:" in str(m.reply_markup) for m in note)

    # перед игрой — сводка; минусов за молчание тренеру нет; в рейтинге его нет
    await api(ADMIN, "distribute", game_id=gid)
    async with webhook._sessionmaker() as s:
        res = await svc.apply_no_response_penalties(s, await svc.get_game(s, gid))
        assert res, "молчащий игрок получает минус"
        assert not (await svc.open_penalty_points(s, [coach_id])).get(coach_id)
        board = await svc.leaderboard(s, config.now())
        assert coach_id not in {r["id"] for r in board}
        await s.commit()
    summary = [m for m in fake.sent(5) if "Состав:" in m.text]
    assert summary and "Идут — 1: Арман" in summary[-1].text and "Не ответили — 1: Даниял" in summary[-1].text

    # вернуть в игроки
    _, res = await api(ADMIN, "player", user_id=coach_id, op="staff_off")
    assert res["player"]["staff"] is None
    status, _ = await api(COACH, "rsvp", game_id=gid, status="yes")
    assert status == 200


async def test_owner_view_as(team):  # noqa: F811
    await _join(ARMAN)
    tomorrow = (config.now() + timedelta(days=1)).date().isoformat()
    await api(ADMIN, "create_game", date=tomorrow, minutes=20 * 60, kind="game")
    _, st = await api(ADMIN, "state", view_as="player")
    s = st["state"]
    assert s["view_as"] == "player" and s["is_admin"] is False and s["real_owner"] is True
    assert "no_answer" not in s["games"][0] and "schedules" not in s
    status, _ = await api(ADMIN, "players", view_as="player")
    assert status == 403  # права — как у игрока
    _, st = await api(ADMIN, "state", view_as="admin")
    assert st["state"]["is_admin"] is True and st["state"]["is_owner"] is False
    _, st = await api(ADMIN, "state", view_as="staff")
    assert st["state"]["user"]["staff"] and "no_answer" in st["state"]["games"][0]
    status, _ = await api(ADMIN, "rsvp", game_id=st["state"]["games"][0]["id"], status="yes", view_as="staff")
    assert status == 400
    # обычный игрок не может притвориться админом
    _, st = await api(ARMAN, "state", view_as="admin")
    assert st["state"]["is_admin"] is False and st["state"]["view_as"] is None


async def test_late_silent_penalty_button(team):  # noqa: F811
    """Сбор закрыли, а молчащий не попал в расчёт (как Нурик) — админ ставит минус кнопкой, один раз."""
    from datetime import datetime

    from bot.models import Game

    fake = team  # noqa: F811
    nurik_id = await _join(tg_user(6, "Нурик"))
    tomorrow = (config.now() + timedelta(days=1)).date().isoformat()
    _, res = await api(ADMIN, "create_game", date=tomorrow, minutes=20 * 60, kind="training")
    gid = res["state"]["games"][0]["id"]
    await api(ADMIN, "rsvp", game_id=gid, status="yes")
    async with webhook._sessionmaker() as s:
        game = await s.get(Game, gid)
        game.created_at = datetime.utcnow() - timedelta(days=2)
        game.starts_at = config.now() + timedelta(hours=2)  # сбор закрылся (за 5 ч до начала)
        game.penalties_applied = True  # закрыли без Нурика
        (await svc.get_user_by_tg(s, 6)).created_at = datetime.utcnow() - timedelta(days=1)  # был в боте до закрытия
        await s.commit()
    _, st = await api(ADMIN, "state")
    g = st["state"]["games"][0]
    assert [u["name"] for u in g["silent_unpenalized"]] == ["Нурик"]
    status, res = await api(ADMIN, "penalize_silent", game_id=gid)
    assert status == 200 and res["note"] == "Минусы поставлены"
    assert any("минус" in m.text.lower() for m in fake.sent(6))
    assert res["state"]["games"][0]["silent_unpenalized"] == []
    _, res = await api(ADMIN, "penalize_silent", game_id=gid)
    assert res["note"] == "Некому ставить минус"
    async with webhook._sessionmaker() as s:
        assert (await svc.open_penalty_points(s, [nurik_id]))[nurik_id] == config.penalty_points
    status, _ = await api(tg_user(6, "Нурик"), "penalize_silent", game_id=gid)
    assert status == 403
