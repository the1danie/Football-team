"""Посещаемость в PDF: ссылка для админа и тренера, на сутки."""

import time
from datetime import datetime, timedelta

from bot import report_pdf, webhook
from bot.config import config
from bot.models import Game, GameStatus
from tests.test_calendar import fetch
from tests.test_miniapp import ADMIN, api, team, tg_user  # noqa: F401 — фикстура team
from tests.test_webhook import fake  # noqa: F401


async def test_attendance_pdf(team, monkeypatch):  # noqa: F811
    monkeypatch.setattr(config, "public_url", "https://team.example")
    pasha = tg_user(2, "Паша")
    await api(pasha, "register", car=False)
    _, pl = await api(ADMIN, "players")
    await api(ADMIN, "player", user_id=next(p["id"] for p in pl["players"] if p["name"] == "Паша"), op="approve")
    day = (config.now() + timedelta(days=1)).date().isoformat()
    _, res = await api(ADMIN, "create_game", date=day, minutes=20 * 60, kind="training")
    gid = res["state"]["games"][0]["id"]
    await api(ADMIN, "rsvp", game_id=gid, status="yes")
    await api(pasha, "rsvp", game_id=gid, status="no")
    async with webhook._sessionmaker() as s:
        game = await s.get(Game, gid)
        game.created_at = datetime.utcnow() - timedelta(days=3)
        game.starts_at = config.now() - timedelta(days=1)
        game.status = GameStatus.FINISHED
        await s.commit()

    assert (await api(pasha, "attendance_pdf", period="month"))[0] == 403
    status, link = await api(ADMIN, "attendance_pdf", period="month")
    assert status == 200 and link["url"].startswith("https://team.example/api/app?report=")
    token = link["url"].split("report=")[1]
    code, headers, body = await fetch(f"report={token}")
    assert code == 200 and headers[b"content-type"] == b"application/pdf" and body.startswith(b"%PDF")
    assert b"content-disposition" in headers and len(body) > 2000

    # подделка, чужой период и просроченная ссылка — нет
    assert (await fetch(f"report={token[:-1]}x"))[0] == 404
    old = report_pdf.make_token(int(ADMIN["id"]), "month", now=time.time() - 2 * report_pdf.LINK_TTL)
    assert (await fetch(f"report={old}"))[0] == 404
    player_token = report_pdf.make_token(2, "all")  # игрок, не админ — даже с верной подписью
    assert (await fetch(f"report={player_token}"))[0] == 404


def test_pdf_layout_many_players():
    kinds = ["came", "no", "maybe", "silent", "no_show"] * 2
    rows = [{"name": f"Игрок с длинным именем {i}", "manual": i % 3 == 0, "games": 10, "came": i % 10, "no": 1,
             "maybe": 1, "silent": 1, "no_show": 1, "failed": 0, "gave": 0, "rate": (i * 7) % 100, "kinds": kinds}
            for i in range(60)]
    body = report_pdf.build(rows, "FC QALAMGER", "за всё время", "23 октября")
    assert body.startswith(b"%PDF") and body.count(b"/Type /Page\n") >= 2 or b"/Count 2" in body or b"/Count 3" in body
