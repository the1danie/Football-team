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


async def test_balance_pdf_for_team(team, monkeypatch):  # noqa: F811
    """Итог в PDF может скачать и игрок — это то же, что в «Рейтинге»."""
    monkeypatch.setattr(config, "public_url", "https://team.example")
    pasha = tg_user(2, "Паша")
    await api(pasha, "register", car=False)
    _, pl = await api(ADMIN, "players")
    await api(ADMIN, "player", user_id=next(p["id"] for p in pl["players"] if p["name"] == "Паша"), op="approve")
    status, link = await api(pasha, "balance_pdf", period="all")
    assert status == 200
    code, headers, body = await fetch("report=" + link["url"].split("report=")[1])
    assert code == 200 and body.startswith(b"%PDF")
    from urllib.parse import unquote

    assert "Итог" in unquote(headers[b"content-disposition"].decode())
    stranger = report_pdf.make_token(77, "all", kind="bal")  # не из команды
    assert (await fetch(f"report={stranger}"))[0] == 404


async def test_monthly_reports_once(team):  # noqa: F811
    from aiogram.methods import SendDocument

    from bot import monthly

    fake = team  # noqa: F811
    coach = tg_user(5, "Серик")
    await api(coach, "register", car=False)
    _, pl = await api(ADMIN, "players")
    cid = next(p["id"] for p in pl["players"] if p["name"] == "Серик")
    await api(ADMIN, "player", user_id=cid, op="approve")
    await api(ADMIN, "player", user_id=cid, op="staff", name="Тренер")
    bot = webhook.make_bot()
    first = datetime(2026, 11, 1, 10, 5)
    async with webhook._sessionmaker() as s:
        assert not await monthly.send_if_due(bot, s, datetime(2026, 10, 24, 12, 0))  # установили — сентябрь не шлём
        assert not await monthly.send_if_due(bot, s, datetime(2026, 10, 25, 12, 0))
        assert not await monthly.send_if_due(bot, s, datetime(2026, 11, 1, 9, 0))  # 1-е, но рано
        assert await monthly.send_if_due(bot, s, first)
        assert not await monthly.send_if_due(bot, s, first + timedelta(hours=2))  # второй раз — нет
        assert not await monthly.send_if_due(bot, s, datetime(2026, 11, 15, 12, 0))
    docs = [c for c in fake.calls if isinstance(c, SendDocument)]
    assert {c.chat_id for c in docs} == {int(ADMIN["id"]), 5}
    assert len(docs) == 4
    assert any("Посещаемость за октябрь 2026" in c.caption for c in docs)
    assert any("Итог за октябрь 2026" in c.caption for c in docs)
    assert monthly.previous_month(datetime(2026, 1, 1, 10)) == (datetime(2025, 12, 1), datetime(2026, 1, 1))


async def test_monthly_catch_up_if_missed_first_day(team):  # noqa: F811
    from bot import monthly

    bot = webhook.make_bot()
    async with webhook._sessionmaker() as s:
        await monthly.send_if_due(bot, s, datetime(2026, 10, 20, 12, 0))
        assert await monthly.send_if_due(bot, s, datetime(2026, 11, 3, 8, 0))  # 1-го бот спал — догоняем


async def test_inactive_players_shown_with_mark(team, monkeypatch):  # noqa: F811
    """Временно не в составе — в посещаемости видны, с пометкой, внизу и без «требуют внимания»."""
    monkeypatch.setattr(config, "public_url", "https://team.example")
    timur = tg_user(3, "Тимур")
    await api(timur, "register", car=False)
    _, pl = await api(ADMIN, "players")
    tid = next(p["id"] for p in pl["players"] if p["name"] == "Тимур")
    await api(ADMIN, "player", user_id=tid, op="approve")
    day = (config.now() + timedelta(days=1)).date().isoformat()
    _, res = await api(ADMIN, "create_game", date=day, minutes=20 * 60, kind="training")
    gid = res["state"]["games"][0]["id"]
    await api(ADMIN, "rsvp", game_id=gid, status="yes")
    await api(timur, "rsvp", game_id=gid, status="no")
    await api(ADMIN, "player", user_id=tid, op="active")  # убрал из состава
    async with webhook._sessionmaker() as s:
        game = await s.get(Game, gid)
        game.created_at = datetime.utcnow() - timedelta(days=3)
        game.starts_at = config.now() - timedelta(days=1)
        game.status = GameStatus.FINISHED
        await s.commit()
    _, rep = await api(ADMIN, "attendance_report", period="month")
    rows = rep["rows"]
    t = next(r for r in rows if r["id"] == tid)
    assert t["inactive"] is True and t["games"] == 1 and t["no"] == 1
    assert rows[-1]["id"] == tid  # внизу
    _, link = await api(ADMIN, "attendance_pdf", period="month")
    code, _, body = await fetch("report=" + link["url"].split("report=")[1])
    assert code == 200 and body.startswith(b"%PDF")
