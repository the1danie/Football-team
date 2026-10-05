"""Веб-версия: вход в браузере по личной ссылке из бота."""

import json

from bot import webhook
from bot.config import config
from bot.miniapp_api import make_web_token, verify_web_token
from tests.test_bot_flow import Harness, buttons, h  # noqa: F401
from tests.test_miniapp import ADMIN, api, team, tg_user  # noqa: F401
from tests.test_webhook import call, fake  # noqa: F401

TOKEN = "42:TEST"


async def web_api(key, action, **body):
    return await call(
        webhook.miniapp_api_app, "POST", headers={"X-Web-Token": key},
        body=json.dumps({"action": action, **body}).encode(),
    )


def test_web_token_signature():
    key = make_web_token(5, 0, TOKEN)
    assert verify_web_token(key, TOKEN) == {"id": 5, "web_version": 0}
    assert verify_web_token(key, "43:OTHER") is None
    tg_id, version, exp, sig = key.split(".")
    assert verify_web_token(f"6.{version}.{exp}.{sig}", TOKEN) is None  # подменили пользователя
    assert verify_web_token(make_web_token(5, 0, TOKEN, days=-1), TOKEN) is None  # просрочено
    assert verify_web_token("мусор", TOKEN) is None and verify_web_token("", TOKEN) is None


async def test_browser_login_and_logout_everywhere(team, monkeypatch):  # noqa: F811
    monkeypatch.setattr(config, "public_url", "https://fb.vercel.app")
    # в Telegram: получить личную ссылку
    _, res = await api(ADMIN, "web_link")
    url = res["url"]
    assert url.startswith("https://fb.vercel.app/app?key=")
    key = url.split("key=")[1]

    # в браузере по ссылке — тот же аккаунт и права
    status, res = await web_api(key, "state")
    assert status == 200 and res["state"]["web"] is True and res["state"]["is_admin"] is True
    assert res["state"]["user"]["name"] == "Даниял"
    status, _ = await web_api(key, "players")
    assert status == 200

    # без ключа или с чужим — нельзя
    status, body = await call(webhook.miniapp_api_app, "POST", body=b'{"action":"state"}')
    assert status == 401 and "/web" in body["error"]
    status, _ = await web_api(make_web_token(999, 0, TOKEN), "state")
    assert status == 401
    # регистрация через сайт закрыта
    status, body = await web_api(make_web_token(77, 0, TOKEN), "register", car=True)
    assert status == 401 or "через бота" in body["error"]

    # «Выйти везде» — все старые ссылки недействительны, новая работает
    status, _ = await web_api(key, "web_logout")
    assert status == 200
    status, body = await web_api(key, "state")
    assert status == 401 and "устарела" in body["error"]
    _, res = await api(ADMIN, "web_link")
    status, _ = await web_api(res["url"].split("key=")[1], "state")
    assert status == 200


async def test_bot_web_command(h: Harness, monkeypatch):  # noqa: F811
    monkeypatch.setattr(config, "public_url", "https://fb.vercel.app")
    monkeypatch.setattr(config, "bot_token", TOKEN)
    await h.register(1, "Даниял", car=True)
    h.fake.reset()
    await h.text(1, "/web")
    msg = h.fake.sent(1)[-1]
    assert "личная ссылка" in msg.text.lower() and "Не пересылай" in msg.text
    link = buttons(msg.reply_markup)[0].url
    assert link.startswith("https://fb.vercel.app/app?key=")
    assert verify_web_token(link.split("key=")[1], config.bot_token)["id"] == 1
    # незарегистрированному ссылку не даём
    h.fake.reset()
    h.names[50] = "Чужой"
    await h.text(50, "/web")
    assert not any("app?key=" in (m.text or "") for m in h.fake.sent(50))
