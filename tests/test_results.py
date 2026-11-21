"""Результат игры: счёт — админ, голы/пасы — сам игрок (или админ), рейтинг «Гол+пас», кнопки в боте."""

from datetime import timedelta

from bot import webhook
from bot.config import config
from bot.models import Game
from bot.scheduler import tick
from tests.test_discipline import _member
from tests.test_late import _game_starting
from tests.test_miniapp import ADMIN, api, team, tg_user  # noqa: F401 — фикстура team
from tests.test_webhook import fake  # noqa: F401


async def _played_game(*players, kind="game"):
    day = (config.now() + timedelta(days=1)).date().isoformat()
    _, res = await api(ADMIN, "create_game", date=day, minutes=20 * 60, kind=kind, opponent="Барыс")
    gid = max(g["id"] for g in res["state"]["games"])
    for p in players:
        await api(p, "rsvp", game_id=gid, status="yes")
    async with webhook._sessionmaker() as s:
        (await s.get(Game, gid)).starts_at = config.now() - timedelta(hours=1)
        await s.commit()
    return gid


async def test_score_goals_assists_and_rating(team):  # noqa: F811
    rustam, nurik, chira = tg_user(2, "Рустам"), tg_user(3, "Нурик"), tg_user(4, "Чира")
    rid = await _member(rustam)
    await _member(nurik)
    await _member(chira)
    gid = await _played_game(rustam, nurik)

    assert (await api(rustam, "result_score", game_id=gid, us=3, them=1))[0] == 403  # счёт — админ
    status, res = await api(rustam, "result_stat", game_id=gid, goals=2, assists=0)
    assert status == 200
    await api(nurik, "result_stat", game_id=gid, goals=0, assists=2)
    assert (await api(chira, "result_stat", game_id=gid, goals=1, assists=0))[0] == 400  # не был на игре
    assert (await api(nurik, "result_stat", game_id=gid, user_id=rid, goals=5))[0] == 403  # чужое — админ

    status, res = await api(ADMIN, "result_score", game_id=gid, us=1, them=0)
    assert status == 400 and "больше" in res["error"]  # голов уже 2
    _, res = await api(ADMIN, "result_score", game_id=gid, us=3, them=1)
    g = next(x for x in res["state"]["games"] if x["id"] == gid)
    r = g["result"]
    assert (r["us"], r["them"], r["outcome"]) == (3, 1, "победа")
    assert [(x["name"], x["goals"], x["assists"]) for x in r["scorers"]] == [("Рустам", 2, 0), ("Нурик", 0, 2)]
    status, res = await api(nurik, "result_stat", game_id=gid, goals=2, assists=2)
    assert status == 400 and "забила 3" in res["error"]  # 2 + 2 > 3
    await api(ADMIN, "result_stat", game_id=gid, user_id=rid, goals=3, assists=0)  # админ поправил

    _, wa = await api(ADMIN, "result_whatsapp", game_id=gid)
    assert "Счёт: 3:1 — победа" in wa["whatsapp"]["text"] and "Рустам 3" in wa["whatsapp"]["text"]
    _, st = await api(ADMIN, "stats", period="all")
    row = next(x for x in st["board"] if x["name"] == "Рустам")
    assert (row["goals"], row["ga"]) == (3, 3)
    assert next(x for x in st["board"] if x["name"] == "Нурик")["ga"] == 2


async def test_training_has_no_result_and_not_before_start(team):  # noqa: F811
    rustam = tg_user(2, "Рустам")
    await _member(rustam)
    tid = await _played_game(rustam, kind="training")
    _, st = await api(rustam, "state")
    assert next(x for x in st["state"]["games"] if x["id"] == tid)["result"] is None
    assert (await api(ADMIN, "result_score", game_id=tid, us=1, them=0))[0] == 400
    gid = await _game_starting(120, rustam)  # тренировка из test_late — тоже без результата
    assert (await api(rustam, "result_stat", game_id=gid, goals=1))[0] == 400


async def test_bot_asks_result_after_game(team):  # noqa: F811
    fake = team  # noqa: F811
    rustam = tg_user(2, "Рустам")
    await _member(rustam)
    gid = await _played_game(rustam)
    async with webhook._sessionmaker() as s:
        (await s.get(Game, gid)).starts_at = config.now() - timedelta(hours=config.finish_after_hours + 1)
        await s.commit()
    fake.reset()
    async with webhook._sessionmaker() as s:
        await tick(webhook.make_bot(), s)
        await s.commit()
    assert any("как сыграли" in m.text for m in fake.sent(1))
    assert any("Забил или отдал голевой пас" in m.text for m in fake.sent(2))
    fake.reset()
    async with webhook._sessionmaker() as s:  # второй раз не спрашивает
        await tick(webhook.make_bot(), s)
        await s.commit()
    assert not any("как сыграли" in m.text for m in fake.sent(1))


async def test_admin_sets_assist_for_anyone_and_player_card_shows_ga(team):  # noqa: F811
    rustam, beka = tg_user(2, "Рустам"), tg_user(3, "Бека")
    rid = await _member(rustam)
    bid = await _member(beka)
    gid = await _played_game(rustam)  # Бека не отметился «Буду», но играл

    _, res = await api(ADMIN, "state")
    g = next(x for x in res["state"]["games"] if x["id"] == gid)
    assert bid in [u["id"] for u in g["team"]]  # админ может выбрать его в окне голов и пасов
    await api(ADMIN, "result_score", game_id=gid, us=3, them=5)
    assert (await api(ADMIN, "result_stat", game_id=gid, user_id=rid, goals=2, assists=0))[0] == 200
    assert (await api(ADMIN, "result_stat", game_id=gid, user_id=bid, goals=1, assists=2))[0] == 200

    _, card = await api(rustam, "player_stats", user_id=bid, period="month")
    assert (card["ga"]["goals"], card["ga"]["assists"]) == (1, 2)
    assert card["ga"]["games"][0]["score"] == "3:5" and "Барыс" in card["ga"]["games"][0]["game"]
