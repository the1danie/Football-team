"""Уведомления на сайте (Web Push) и вход по имени + PIN."""

import json
import os
import struct
from datetime import timedelta

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from sqlalchemy import select

from bot import webhook, webpush
from bot.config import config
from bot.models import PushSubscription, User
from bot.services import games as svc
from bot.weblink import make_web_token
from tests.test_miniapp import ADMIN, api, team, tg_user  # noqa: F401 — фикстура team
from tests.test_webhook import call, fake  # noqa: F401


def browser_keys():
    priv = ec.generate_private_key(ec.SECP256R1())
    pub = priv.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    auth = os.urandom(16)
    return priv, webpush.b64u(pub), webpush.b64u(auth)


def decrypt(body: bytes, priv, p256dh: str, auth: str) -> bytes:
    """Как это делает браузер (RFC 8291)."""
    salt, rs, idlen = body[:16], *struct.unpack("!IB", body[16:21])
    as_public, cipher = body[21:21 + idlen], body[21 + idlen:]
    assert rs == 4096
    shared = priv.exchange(ec.ECDH(), ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), as_public))
    ua_public = webpush.unb64u(p256dh)
    ikm = webpush._hkdf(webpush.unb64u(auth), shared, b"WebPush: info\x00" + ua_public + as_public, 32)
    cek = webpush._hkdf(salt, ikm, b"Content-Encoding: aes128gcm\x00", 16)
    nonce = webpush._hkdf(salt, ikm, b"Content-Encoding: nonce\x00", 12)
    plain = AESGCM(cek).decrypt(nonce, cipher, None)
    assert plain.endswith(b"\x02")
    return plain[:-1]


def test_encrypt_roundtrip_and_vapid():
    priv, p256dh, auth = browser_keys()
    msg = json.dumps({"title": "📣 Открыт сбор", "body": "Тренировка"}, ensure_ascii=False).encode()
    assert decrypt(webpush.encrypt(msg, p256dh, auth), priv, p256dh, auth) == msg
    key = ec.generate_private_key(ec.SECP256R1())
    header = webpush.vapid_header("https://fcm.googleapis.com/fcm/send/abc", key)
    token = header.split("t=")[1].split(",")[0]
    claims = json.loads(webpush.unb64u(token.split(".")[1]))
    assert claims["aud"] == "https://fcm.googleapis.com" and len(webpush.unb64u(token.split(".")[2])) == 64
    m = webpush.message("<b>📣 Открыт сбор</b>\n\nТренировка — 9 окт., 23:00\nОтметься до 20:00")
    assert m["title"] == "📣 Открыт сбор" and "Тренировка" in m["body"] and "<b>" not in m["title"]


async def test_subscribe_and_receive_push(team, monkeypatch):  # noqa: F811
    posted = []
    status = {"code": 201}

    async def fake_post(endpoint, body, headers):
        posted.append((endpoint, body, headers))
        return status["code"]

    monkeypatch.setattr(webpush, "_post", fake_post)
    monkeypatch.setattr(webpush, "_private", None)
    priv, p256dh, auth = browser_keys()
    # игрок без Telegram (добавлен вручную) заходит по ссылке на сайт и включает уведомления
    _, res = await api(ADMIN, "player_add", name="Нурик", car=False)
    nurik_id = res["player"]["id"]
    async with webhook._sessionmaker() as s:
        tg_id = (await s.get(User, nurik_id)).telegram_id
    web = {"X-Web-Token": make_web_token(tg_id, 0, "42:TEST"), "Content-Type": "application/json"}

    async def web_api(action, **body):
        return await call(webhook.miniapp_api_app, "POST", headers=web, body=json.dumps({"action": action, **body}).encode())

    status_code, k = await web_api("push_key")
    assert status_code == 200 and len(webpush.unb64u(k["key"])) == 65 and k["subscribed"] == 0
    endpoint = "https://fcm.googleapis.com/fcm/send/xyz"
    _, res = await web_api("push_subscribe", subscription={"endpoint": endpoint, "keys": {"p256dh": p256dh, "auth": auth}})
    assert res["note"] == "🔔 Уведомления включены"
    hello = json.loads(decrypt(posted[-1][1], priv, p256dh, auth))
    assert hello["title"] == "🔔 Уведомления включены"
    assert posted[-1][2]["Content-Encoding"] == "aes128gcm" and posted[-1][2]["Authorization"].startswith("vapid t=")

    # открыли сбор — ему приходит уведомление (в Telegram его нет)
    tomorrow = (config.now() + timedelta(days=1)).date().isoformat()
    _, res = await api(ADMIN, "create_game", date=tomorrow, minutes=20 * 60, kind="training")
    assert "Без Telegram" not in res["note"]  # дошло уведомлением
    poll = json.loads(decrypt(posted[-1][1], priv, p256dh, auth))
    assert "сбор" in poll["title"].lower()

    # подписка умерла (удалили иконку) — 410, удаляем её
    status["code"] = 410
    _, res = await api(ADMIN, "create_game", date=tomorrow, minutes=21 * 60, kind="game")
    async with webhook._sessionmaker() as s:
        assert await svc.push_count(s, nurik_id) == 0
        assert (await s.scalars(select(PushSubscription))).all() == []


async def test_pin_login_and_lockout(team):  # noqa: F811
    pasha = tg_user(2, "Паша")
    await api(pasha, "register", car=False)
    _, pl = await api(ADMIN, "players")
    pid = next(p["id"] for p in pl["players"] if p["name"] == "Паша")
    await api(ADMIN, "player", user_id=pid, op="approve")
    _, res = await api(ADMIN, "player_pin", user_id=pid)
    pin = res["pin"]
    assert len(pin) == 4 and pin in res["text"] and "Паша" in res["text"]
    assert (await api(pasha, "player_pin", user_id=pid))[0] == 403

    async def login(name, p):
        return await call(webhook.miniapp_api_app, "POST", headers={"Content-Type": "application/json"},
                          body=json.dumps({"action": "pin_login", "name": name, "pin": p}).encode())

    status, body = await login(" паша ", pin)
    assert status == 200 and body["key"]
    status, st = await call(webhook.miniapp_api_app, "POST", headers={"X-Web-Token": body["key"]},
                            body=b'{"action":"state"}')
    assert st["state"]["user"]["name"] == "Паша" and st["state"]["web"] is True
    assert (await login("Никто", pin))[0] == 403
    wrong = "0000" if pin != "0000" else "1111"
    for _ in range(5):
        assert (await login("Паша", wrong))[0] == 403
    status, body = await login("Паша", pin)  # заблокирован после 5 ошибок — даже верный PIN
    assert status == 429 and "Слишком много попыток" in body["error"]
    _, res = await api(ADMIN, "player_pin", user_id=pid)  # админ выдал новый — блок снят
    assert (await login("Паша", res["pin"]))[0] == 200
    assert (await login("Паша", pin))[0] == 403 or res["pin"] == pin  # старый PIN больше не работает


async def test_own_pin_from_profile(team):  # noqa: F811
    _, res = await api(ADMIN, "my_pin")
    assert len(res["pin"]) == 4 and res["name"] == "Даниял"
    status, body = await call(webhook.miniapp_api_app, "POST", headers={"Content-Type": "application/json"},
                              body=json.dumps({"action": "pin_login", "name": "Даниял", "pin": res["pin"]}).encode())
    assert status == 200 and body["key"]
