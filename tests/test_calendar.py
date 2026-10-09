"""Подписка на календарь (.ics) для главного админа."""

from datetime import timedelta

from bot import calendar_feed, webhook
from bot.config import config
from tests.test_miniapp import ADMIN, api, team, tg_user  # noqa: F401 — фикстура team
from tests.test_webhook import fake  # noqa: F401


async def fetch(query: str):
    sent = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(m):
        sent.append(m)

    await webhook.app_page({"type": "http", "method": "GET", "headers": [], "query_string": query.encode()}, receive, send)
    return sent[0]["status"], dict(sent[0]["headers"]), sent[1]["body"]


async def test_owner_calendar_feed(team, monkeypatch):  # noqa: F811
    monkeypatch.setattr(config, "public_url", "https://team.example")
    pasha = tg_user(2, "Паша")
    await api(pasha, "register", car=False)
    _, pl = await api(ADMIN, "players")
    await api(ADMIN, "player", user_id=next(p["id"] for p in pl["players"] if p["name"] == "Паша"), op="approve")
    day = (config.now() + timedelta(days=1)).date().isoformat()
    _, res = await api(ADMIN, "create_game", date=day, minutes=21 * 60 + 30, kind="training", location="Жас Оркен")
    gid = res["state"]["games"][0]["id"]
    await api(ADMIN, "rsvp", game_id=gid, status="yes")
    far = (config.now() + timedelta(days=5)).date().isoformat()
    await api(ADMIN, "create_game", date=far, minutes=20 * 60, kind="training", repeat=True, open_days_before=2)
    _, res = await api(ADMIN, "create_game", date=day, minutes=19 * 60, kind="game")
    cancelled = max(g["id"] for g in res["state"]["games"])
    await api(ADMIN, "cancel", game_id=cancelled)

    _, link = await api(ADMIN, "calendar_link")
    assert link["url"].startswith("https://team.example/api/app?ics=1.") and link["webcal"].startswith("webcal://")
    assert (await api(pasha, "calendar_link"))[0] == 403

    status, headers, body = await fetch(link["url"].split("?", 1)[1])
    assert status == 200 and headers[b"content-type"].startswith(b"text/calendar")
    ics = body.decode()
    assert ics.startswith("BEGIN:VCALENDAR\r\n") and ics.endswith("END:VCALENDAR\r\n")
    assert all(len(line.encode()) <= 75 for line in ics.split("\r\n"))
    unfolded = ics.replace("\r\n ", "")
    assert f"UID:game-{gid}@football-team" in unfolded and "SUMMARY:🏃 Тренировка — Жас Оркен" in unfolded
    assert "Идут: 1 (Даниял)" in unfolded and "LOCATION:Жас Оркен" in unfolded
    assert f"UID:game-{cancelled}@football-team" in unfolded and "STATUS:CANCELLED" in unfolded
    assert unfolded.count("UID:schedule-") >= 7  # тренировки по расписанию на недели вперёд

    # чужая или поддельная ссылка — 404
    assert (await fetch("ics=2." + calendar_feed._sig(2)))[0] == 404  # подпись верная, но не главный админ
    assert (await fetch("ics=1.deadbeef"))[0] == 404
