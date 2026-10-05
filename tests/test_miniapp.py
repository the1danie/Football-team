"""Mini App: подпись Telegram, права доступа, сценарии через API."""

import json
import time
from datetime import timedelta

import pytest

from bot import webhook
from bot.config import config
from bot.miniapp_api import sign_init_data, verify_init_data
from bot.services import games as svc
from tests.test_webhook import call, fake  # noqa: F401 — фикстура fake: тестовая БД и фейковый Telegram

TOKEN = "42:TEST"
ADMIN = {"id": 1, "first_name": "Даниял", "username": "daniyal"}


def tg_user(uid, name, **extra):
    return {"id": uid, "first_name": name, **extra}


async def api(user, action, **body):
    headers = {"X-Telegram-Init-Data": sign_init_data(user, TOKEN), "Content-Type": "application/json"}
    status, data = await call(webhook.miniapp_api_app, "POST", headers=headers,
                              body=json.dumps({"action": action, **body}).encode())
    return status, data


def test_init_data_signature():
    good = sign_init_data(ADMIN, TOKEN)
    assert verify_init_data(good, TOKEN)["id"] == 1
    assert verify_init_data(good, "43:OTHER") is None  # чужой токен
    tampered = good.replace("%D0%94%D0%B0%D0%BD", "%D0%92%D0%B0%D1%81")  # подменили имя
    assert verify_init_data(tampered, TOKEN) is None
    old = sign_init_data(ADMIN, TOKEN, auth_date=int(time.time()) - 3 * 24 * 3600)
    assert verify_init_data(old, TOKEN) is None  # просрочено
    assert verify_init_data("", TOKEN) is None


async def test_page_and_auth(fake):  # noqa: F811
    sent = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(m):
        sent.append(m)

    await webhook.app_page({"type": "http", "method": "GET", "headers": [], "query_string": b""}, receive, send)
    assert sent[0]["status"] == 200 and (b"content-type", b"text/html; charset=utf-8") in sent[0]["headers"]
    assert "telegram-web-app.js" in sent[1]["body"].decode()
    status, body = await call(webhook.miniapp_api_app, "POST", body=b'{"action":"state"}')
    assert status == 401 and "из Telegram" in body["error"]


@pytest.fixture
async def team(fake, monkeypatch):  # noqa: F811
    monkeypatch.setattr(config, "admin_ids", [1])
    # админ регистрируется через приложение
    _, st = await api(ADMIN, "state")
    assert st["state"]["access"] == "new" and st["state"]["tg_name"] == "Даниял"
    _, st = await api(ADMIN, "register", car=True)
    assert st["state"]["access"] == "ok" and st["state"]["is_admin"] is True
    return fake


async def test_full_miniapp_flow(team):
    fake = team  # noqa: F811
    arman = tg_user(2, "Арман", username="arman")
    timur = tg_user(3, "Тимур")

    # --- новичок: регистрация → заявка админу → до подтверждения ничего нельзя
    _, st = await api(arman, "register", car=False, name="Арман")
    assert st["state"]["access"] == "pending"
    assert any("Новый игрок" in m.text for m in fake.sent(1))
    status, body = await api(arman, "stats")
    assert status == 403 and "Заявка у администратора" in body["error"]
    status, _ = await api(arman, "players")
    assert status == 403

    # --- админ видит заявку и принимает
    _, st = await api(ADMIN, "state")
    assert st["state"]["pending_count"] == 1
    _, pl = await api(ADMIN, "players")
    arman_id = next(p["id"] for p in pl["players"] if p["name"] == "Арман")
    assert next(p for p in pl["players"] if p["id"] == arman_id)["status"] == "pending"
    _, res = await api(ADMIN, "player", user_id=arman_id, op="approve")
    assert res["player"]["status"] == "approved"
    assert any("добавил тебя в команду" in m.text for m in fake.sent(2))
    await api(timur, "register", car=True)
    _, pl = await api(ADMIN, "players")
    timur_id = next(p["id"] for p in pl["players"] if p["name"] == "Тимур")
    await api(ADMIN, "player", user_id=timur_id, op="approve")

    # --- не-админ не может создать игру
    status, _ = await api(arman, "create_game", date="2030-01-01", minutes=1200, kind="game")
    assert status == 403

    # --- админ создаёт игру: опрос в личку, анонс для WhatsApp
    tomorrow = (config.now() + timedelta(days=1)).date().isoformat()
    status, res = await api(ADMIN, "create_game", date=tomorrow, minutes=20 * 60 + 30, kind="game", location="Динамо")
    assert status == 200, res
    assert res["whatsapp"]["url"].startswith("https://wa.me/?text=")
    assert "start=game_" in res["whatsapp"]["text"] and "Опрос отправлен" in res["note"]
    game = res["state"]["games"][0]
    assert game["time"] == "20:30" and game["location"] == "Динамо" and game["status"] == "open"
    assert {u["name"] for u in game["no_answer"]} == {"Даниял", "Арман", "Тимур"}
    assert any("Открыт сбор" in m.text for m in fake.sent(2))
    gid = game["id"]

    # --- отметки
    for user in (ADMIN, arman, timur):
        status, res = await api(user, "rsvp", game_id=gid, status="yes")
        assert status == 200
    g = res["state"]["games"][0]
    assert g["my_rsvp"] == "yes" and len(g["participants"]["yes"]) == 3
    assert "no_answer" not in g  # игроку список молчащих не показываем
    status, body = await api(arman, "rsvp", game_id=gid, status="nope")
    assert status == 400

    # --- распределение и назначения
    _, res = await api(ADMIN, "distribute", game_id=gid)
    g = res["state"]["games"][0]
    assert g["status"] == "distributed" and all(d["user"] for d in g["duties"])
    balls = next(d for d in g["duties"] if d["name"] == "Мячи")
    assert balls["user"]["car"] is True
    status, body = await api(ADMIN, "assign", game_id=gid, duty_id=balls["duty_id"], user_id=arman_id)
    assert status == 400 and "нужна машина" in body["error"]
    _, res = await api(ADMIN, "assign", game_id=gid, duty_id=balls["duty_id"], user_id=timur_id)
    assert next(d for d in res["state"]["games"][0]["duties"] if d["name"] == "Мячи")["user"]["name"] == "Тимур"

    # --- обмен: Арман предлагает свою обязанность
    _, st = await api(arman, "state")
    mine = next(d for d in st["state"]["games"][0]["duties"] if d["mine"])
    _, tr = await api(arman, "swap_targets", assignment_id=mine["assignment_id"])
    assert tr["targets"]
    target = tr["targets"][0]
    _, res = await api(arman, "swap", assignment_id=mine["assignment_id"], user_id=target["id"])
    assert "Предложение отправлено" in res["note"]
    status, _ = await api(timur, "swap_targets", assignment_id=mine["assignment_id"])  # чужое назначение
    assert status == 400

    # --- WhatsApp-текст, статистика
    _, wa = await api(ADMIN, "whatsapp", game_id=gid)
    assert "*Обязанности*" in wa["text"]
    _, stats = await api(arman, "stats")
    assert {p["name"] for p in stats["team"]} == {"Даниял", "Арман", "Тимур"}
    _, ps = await api(arman, "player_stats", user_id=timur_id)
    assert "telegram_id" not in ps  # игроку — без служебных полей

    # --- машина: админ закрепил — игрок изменить не может
    await api(ADMIN, "player", user_id=arman_id, op="car_on")
    status, body = await api(arman, "profile", car=False)
    assert status == 400 and "только он" in body["error"]
    _, res = await api(arman, "profile", name="Арман К.")
    assert res["state"]["user"]["name"] == "Арман К." and res["state"]["user"]["car_locked"] is True

    # --- удаление из команды: доступ закрыт, обязанности ушли
    _, res = await api(ADMIN, "player", user_id=arman_id, op="block")
    status, body = await api(arman, "state")
    assert body["state"]["access"] == "blocked"
    status, _ = await api(arman, "rsvp", game_id=gid, status="yes")
    assert status == 403
    async with webhook._sessionmaker() as s:
        assert arman_id not in {a.user_id for a in await svc.active_assignments(s, gid)}

    # --- отмена игры
    _, res = await api(ADMIN, "cancel", game_id=gid)
    assert res["state"]["games"] == []


def test_app_buttons_only_with_public_url(monkeypatch):
    from bot import keyboards

    monkeypatch.setattr(config, "public_url", "")
    assert keyboards.app_button(5) is None  # локальный запуск без HTTPS — кнопки нет
    monkeypatch.setattr(config, "public_url", "https://fb.vercel.app")
    button = keyboards.app_button(5)
    assert button.web_app.url == "https://fb.vercel.app/app?game=5"
    markup = keyboards.with_app_button(keyboards.rsvp(svc.Game(id=5, status="open")), 5)
    assert [len(r) for r in markup.inline_keyboard] == [3, 1]
