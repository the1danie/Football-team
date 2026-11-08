"""План на месяц: ведут тренер/капитан/админ, видят все; замеры на тренировках, голы со стандартов в матчах."""

from datetime import timedelta

from bot import webhook
from bot.config import config
from bot.models import Game
from tests.test_miniapp import ADMIN, api, team, tg_user  # noqa: F401 — фикстура team
from tests.test_webhook import fake  # noqa: F401


async def _member(user):
    await api(user, "register", car=False)
    _, pl = await api(ADMIN, "players")
    uid = next(p["id"] for p in pl["players"] if p["name"] == user["first_name"])
    await api(ADMIN, "player", user_id=uid, op="approve")
    return uid


async def _create(kind, days):
    day = (config.now() + timedelta(days=days)).date().isoformat()
    _, res = await api(ADMIN, "create_game", date=day, minutes=20 * 60, kind=kind)
    return max(g["id"] for g in res["state"]["games"])


def _body(template, **extra):
    today = config.now().date()
    return {"title": "Ноябрь", "starts_on": (today - timedelta(days=3)).isoformat(),
            "ends_on": (today + timedelta(days=27)).isoformat(), "trainings": 8, "set_piece_goal": 2,
            "tasks": template["tasks"], "plans": {"1": "Угловые: комбинации А и Б", "2": "Ауты"}, **extra}


async def test_plan_rights_progress_and_goals(team):  # noqa: F811
    meir, arman = tg_user(2, "Мейр"), tg_user(3, "Арман")
    meir_id = await _member(meir)
    await _member(arman)

    status, r = await api(meir, "cycle")
    assert status == 200 and r["cycle"] is None and not r["planner"]
    template = r["template"]
    status, _ = await api(meir, "cycle_save", **_body(template))
    assert status == 403  # игрок не ведёт план

    await api(ADMIN, "player", user_id=meir_id, op="captain_on")
    _, st = await api(meir, "state")
    assert st["state"]["user"]["captain"] and st["state"]["user"]["planner"]
    status, r = await api(meir, "cycle_save", **_body(template))
    assert status == 200, r
    c = r["cycle"]
    assert [t["title"] for t in c["tasks"]] == [t["title"] for t in template["tasks"]]
    assert c["trainings_done"] == 0 and c["set_piece_goal"] == 2

    # тренировки в датах плана нумеруются по порядку, план показывается в карточке
    t1 = await _create("training", 1)
    t2 = await _create("training", 2)
    match = await _create("match", 3)
    _, st = await api(arman, "state")
    cards = {g["id"]: g for g in st["state"]["games"]}
    assert cards[t1]["plan"] == {"number": 1, "of": 8, "text": "Угловые: комбинации А и Б"}
    assert cards[t2]["plan"]["number"] == 2 and cards[match].get("plan") is None
    assert cards[match]["kind_title"] == "🏆 Матч"

    # замеры
    _, r = await api(ADMIN, "cycle")
    task = r["cycle"]["tasks"][0]["id"]
    status, _ = await api(arman, "cycle_measure", game_id=t1, task_id=task, attempts=10, success=4)
    assert status == 403
    status, r = await api(meir, "cycle_measure", game_id=t1, task_id=task, attempts=5, success=6)
    assert status == 400  # удачных больше, чем попыток
    await api(meir, "cycle_measure", game_id=t1, task_id=task, attempts=10, success=4)
    _, r = await api(ADMIN, "cycle_measure", game_id=t2, task_id=task, attempts=10, success=7)
    t = r["cycle"]["tasks"][0]
    assert (t["success"], t["attempts"], t["rate"], t["first"], t["last"]) == (11, 20, 55, 40, 70)

    # голы в матче: 3 со стандартов (2 угловых + 1 штрафной) — цель выполнена
    await api(meir, "cycle_goals", game_id=match, side="for", source="corner", count=2)
    await api(meir, "cycle_goals", game_id=match, side="for", source="free_kick", count=1)
    await api(meir, "cycle_goals", game_id=match, side="for", source="play", count=1)
    _, r = await api(meir, "cycle_goals", game_id=match, side="against", source="play", count=2)
    m = r["cycle"]["matches"]
    assert (m["count"], m["set_pieces"], m["avg"], m["hit"]) == (1, 3, 3.0, 1)
    s = next(x for x in r["cycle"]["sessions"] if x["game_id"] == match)
    assert (s["goals"]["scored"], s["goals"]["conceded"]) == (4, 2)
    assert "3.0 за матч" in r["whatsapp"]["text"] and "40% → стало 70%" in r["whatsapp"]["text"]

    # 0 — удаляет запись; прошедшие тренировки считаются
    await api(meir, "cycle_goals", game_id=match, side="for", source="corner", count=0)
    async with webhook._sessionmaker() as s:
        (await s.get(Game, t1)).starts_at = config.now() - timedelta(hours=3)
        await s.commit()
    _, r = await api(arman, "cycle")
    assert r["cycle"]["matches"]["set_pieces"] == 1 and r["cycle"]["matches"]["hit"] == 0
    assert r["cycle"]["trainings_done"] == 1

    # убрали задачу — её замеры пропали; переименовали — замеры остались
    tasks = [{**x, "title": x["title"] + "!"} for x in r["cycle"]["tasks"][:1]]
    _, r = await api(meir, "cycle_save", **_body(template, id=r["cycle"]["id"], tasks=tasks))
    assert len(r["cycle"]["tasks"]) == 1 and r["cycle"]["tasks"][0]["attempts"] == 20

    await api(ADMIN, "player", user_id=meir_id, op="captain_off")
    status, _ = await api(meir, "cycle_delete", id=r["cycle"]["id"])
    assert status == 403
    _, r = await api(ADMIN, "cycle_delete", id=r["cycle"]["id"])
    assert r["cycle"] is None
