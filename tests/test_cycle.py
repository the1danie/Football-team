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


async def test_combo_library_roles_scheme_and_goals(team):  # noqa: F811
    import base64
    import io

    from PIL import Image

    meir, arman = tg_user(2, "Мейр"), tg_user(3, "Арман")
    meir_id = await _member(meir)
    arman_id = await _member(arman)
    body = {"kind": "corner", "name": "Угловой А", "description": "Подача на ближнюю\nСкидка назад",
            "roles": [{"role": "подаёт", "user_id": meir_id}, {"role": "ближняя штанга", "user_id": arman_id},
                      {"role": "", "user_id": arman_id}]}
    status, _ = await api(meir, "cycle_combo_save", **body)
    assert status == 403  # игрок не ведёт библиотеку
    status, r = await api(ADMIN, "cycle_combo_save", **body)
    assert status == 200, r
    c = r["combos"]["items"][0]
    assert c["name"] == "Угловой А" and c["status"] == "learning" and c["description"].count("\n") == 1
    assert c["roles"] == [{"role": "подаёт", "user_id": meir_id, "name": "Мейр"},
                          {"role": "ближняя штанга", "user_id": arman_id, "name": "Арман"}]
    status, r = await api(ADMIN, "cycle_combo_save", kind="penalty", name="X")
    assert status == 400

    # игроки видят библиотеку
    _, r = await api(arman, "cycle")
    assert r["combos"]["items"][0]["roles"][1]["name"] == "Арман"

    # схема: сохраняется без обрезки и отдаётся по подписанной ссылке
    buf = io.BytesIO()
    Image.new("RGB", (1600, 900), "green").save(buf, "PNG")
    img = "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()
    _, r = await api(ADMIN, "cycle_combo_image", id=c["id"], image=img)
    url = r["combos"]["items"][0]["image"]
    async with webhook._sessionmaker() as s:
        from bot import combos
        data = await combos.load_image(s, url.split("scheme=")[1])
        assert Image.open(io.BytesIO(data)).size == (1200, 675)
        assert await combos.load_image(s, url.split("scheme=")[1][:-1] + "0") is None

    # голы комбинацией в матче
    match = await _create("match", 1)
    await api(ADMIN, "cycle_combo_save", id=c["id"], kind="corner", name="Угловой А", status="ready", roles=body["roles"])
    _, r = await api(ADMIN, "cycle_combo_goals", game_id=match, combo_id=c["id"], count=2)
    item = r["combos"]["items"][0]
    assert (item["goals_matches"], item["status"]) == (2, "ready")
    assert r["combos"]["per_game"][str(match)] == {str(c["id"]): 2}

    _, r = await api(ADMIN, "cycle_combo_delete", id=c["id"])
    assert r["combos"]["items"] == []


async def test_attendance_and_recap_for_absent(team):  # noqa: F811
    fake = team  # noqa: F811
    meir, arman, timur = tg_user(2, "Мейр"), tg_user(3, "Арман"), tg_user(4, "Тимур")
    await _member(meir)
    await _member(arman)
    timur_id = await _member(timur)
    _, r = await api(ADMIN, "cycle")
    _, r = await api(ADMIN, "cycle_save", **_body(r["template"]))
    task = r["cycle"]["tasks"][1]
    gid = await _create("training", 1)
    for u, st in ((meir, "yes"), (arman, "yes"), (timur, "no")):
        await api(u, "rsvp", game_id=gid, status=st)
    status, _ = await api(ADMIN, "cycle_recap", game_id=gid)
    assert status == 400  # ещё не прошла
    async with webhook._sessionmaker() as s:
        (await s.get(Game, gid)).starts_at = config.now() - timedelta(hours=2)
        await s.commit()
    await api(ADMIN, "cycle_measure", game_id=gid, task_id=task["id"], attempts=8, success=2)
    _, r = await api(timur, "cycle")
    sess = next(x for x in r["cycle"]["sessions"] if x["game_id"] == gid)
    came = {u["name"] for u in sess["attendance"]["came"]}
    missed = {u["name"] for u in sess["attendance"]["missed"]}
    assert came == {"Мейр", "Арман"} and "Тимур" in missed and not came & missed
    assert r["cycle"]["avg_attendance"] == 2
    assert "Угловые: комбинации А и Б" in sess["recap"] and "2 из 8 розыгрышей" in sess["recap"]

    status, _ = await api(timur, "cycle_recap", game_id=gid)
    assert status == 403
    _, r = await api(ADMIN, "cycle_recap", game_id=gid)
    assert next(x for x in r["cycle"]["sessions"] if x["game_id"] == gid)["recap_sent"] >= 1
    got = [m.text for m in fake.sent(4) if "Что было без тебя" in m.text]
    assert got and "Были 2: Арман, Мейр" in got[-1]
    assert not any("Что было без тебя" in m.text for m in fake.sent(2))
    assert timur_id
