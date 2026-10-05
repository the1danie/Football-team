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


async def test_web_buttons_everywhere(h: Harness, monkeypatch):  # noqa: F811
    from datetime import timedelta

    from bot import texts
    from bot.services import games as svc

    monkeypatch.setattr(config, "public_url", "https://fb.vercel.app")
    monkeypatch.setattr(config, "bot_token", TOKEN)
    await h.register(1, "Даниял", car=True)
    h.fake.reset()
    await h.register(2, "Арман", car=False)

    # приветствие после подтверждения — с кнопкой сайта
    welcome = [m for m in h.fake.sent(2) if m.reply_markup and "сайт" in m.text.lower()]
    assert welcome and buttons(welcome[0].reply_markup)[-1].text == "🌐 Открыть на сайте"
    # меню: кнопка «🌐 Сайт»
    menu = next(m for m in h.fake.sent(2) if "добавил тебя в команду" in m.text)
    assert any(b.text == texts.BTN_WEB for row in menu.reply_markup.keyboard for b in row)
    h.fake.reset()
    await h.text(2, texts.BTN_WEB)
    link = buttons(h.fake.sent(2)[-1].reply_markup)[0].url
    assert verify_web_token(link.split("key=")[1], TOKEN)["id"] == 2

    # опрос «Открыт сбор» — с личной ссылкой на сайт
    h.fake.reset()
    async with h.sm() as s:
        creator = await svc.get_user_by_tg(s, 1)
        from bot import actions

        game = await svc.create_game(s, "training", config.now() + timedelta(days=1), None, creator)
        await actions.send_poll_invites(h.bot, s, game, skip=creator)
        await s.commit()
    invite = next(m for m in h.fake.sent(2) if "Открыт сбор" in m.text)
    site = buttons(invite.reply_markup)[-1]
    assert site.text == "🌐 Открыть на сайте" and verify_web_token(site.url.split("key=")[1], TOKEN)["id"] == 2

    # /invite — инструкция для WhatsApp (только админ)
    h.fake.reset()
    await h.text(1, "/invite")
    draft = h.fake.sent(1)[-1]
    assert "https://t.me/duty_bot" in draft.text and "Сайт" in draft.text and draft.parse_mode is None
    h.fake.reset()
    await h.text(2, "/invite")
    assert not any("Как отмечаться" in (m.text or "") for m in h.fake.sent(2))


async def test_team_invite_api(team):  # noqa: F811
    _, res = await api(ADMIN, "team_invite")
    assert "Как отмечаться" in res["text"] and res["url"].startswith("https://wa.me/")
    status, _ = await api(tg_user(9, "Игрок"), "team_invite")
    assert status == 403


async def test_whatsapp_texts_link_to_site(fake, monkeypatch):  # noqa: F811
    """Тексты для WhatsApp: и бот в Telegram, и сайт — для тех, кто без Telegram."""
    from bot import whatsapp
    from bot.models import Game

    game = Game(id=4, kind="training", status="open")
    monkeypatch.setattr(config, "public_url", "https://qalamger.vercel.app")
    links = await whatsapp.game_link(webhook.make_bot(), game)
    assert links == "Telegram: https://t.me/duty_bot?start=game_4\nСайт: https://qalamger.vercel.app/app?game=4"
    assert "key=" not in links  # в группу — только общая ссылка, без личного ключа
    assert "Сайт команды: https://qalamger.vercel.app/app" in whatsapp.team_invite("duty_bot")

    monkeypatch.setattr(config, "public_url", "")  # без сайта — только бот
    assert await whatsapp.game_link(webhook.make_bot(), game) == "https://t.me/duty_bot?start=game_4"
