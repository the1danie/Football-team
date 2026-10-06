"""Web Push: уведомления на сайте (Android, компьютер; iPhone — если сайт добавлен на экран «Домой»).

Без внешних библиотек web-push: шифрование aes128gcm (RFC 8291) и подпись VAPID (RFC 8292)
на `cryptography`, отправка — aiohttp. Ключи VAPID бот создаёт сам и хранит в настройках БД.
"""

import asyncio
import base64
import json
import logging
import os
import re
import struct
import time
from urllib.parse import urlparse

import aiohttp
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.models import PushSubscription, User

log = logging.getLogger(__name__)

VAPID_SETTING = "vapid_private_key"
VAPID_SUBJECT = "mailto:team-bot@example.com"
_private: ec.EllipticCurvePrivateKey | None = None


def b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def unb64u(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _raw_public(key: ec.EllipticCurvePrivateKey | ec.EllipticCurvePublicKey) -> bytes:
    pub = key.public_key() if isinstance(key, ec.EllipticCurvePrivateKey) else key
    return pub.public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)


async def vapid_key(session: AsyncSession) -> ec.EllipticCurvePrivateKey:
    """Ключ VAPID: из переменной VAPID_PRIVATE_KEY, иначе из БД (создаётся при первом обращении)."""
    global _private
    if _private is not None:
        return _private
    from bot.services import games as svc  # noqa: PLC0415

    raw = os.getenv("VAPID_PRIVATE_KEY") or await svc.get_setting(session, VAPID_SETTING)
    if raw:
        _private = ec.derive_private_key(int.from_bytes(unb64u(raw), "big"), ec.SECP256R1())
    else:
        _private = ec.generate_private_key(ec.SECP256R1())
        await svc.set_setting(session, VAPID_SETTING, b64u(_private.private_numbers().private_value.to_bytes(32, "big")))
    return _private


async def public_key(session: AsyncSession) -> str:
    return b64u(_raw_public(await vapid_key(session)))


def _hkdf(salt: bytes, ikm: bytes, info: bytes, length: int) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=length, salt=salt, info=info).derive(ikm)


def encrypt(payload: bytes, p256dh: str, auth: str) -> bytes:
    """Тело запроса aes128gcm для подписки браузера (RFC 8291)."""
    ua_public = unb64u(p256dh)
    auth_secret = unb64u(auth)
    as_private = ec.generate_private_key(ec.SECP256R1())
    as_public = _raw_public(as_private)
    shared = as_private.exchange(ec.ECDH(), ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), ua_public))
    ikm = _hkdf(auth_secret, shared, b"WebPush: info\x00" + ua_public + as_public, 32)
    salt = os.urandom(16)
    cek = _hkdf(salt, ikm, b"Content-Encoding: aes128gcm\x00", 16)
    nonce = _hkdf(salt, ikm, b"Content-Encoding: nonce\x00", 12)
    cipher = AESGCM(cek).encrypt(nonce, payload + b"\x02", None)
    return salt + struct.pack("!IB", 4096, len(as_public)) + as_public + cipher


def vapid_header(endpoint: str, key: ec.EllipticCurvePrivateKey) -> str:
    url = urlparse(endpoint)
    head = b64u(json.dumps({"typ": "JWT", "alg": "ES256"}).encode())
    claims = b64u(json.dumps({"aud": f"{url.scheme}://{url.netloc}", "exp": int(time.time()) + 12 * 3600,
                              "sub": VAPID_SUBJECT}).encode())
    r, s = decode_dss_signature(key.sign(f"{head}.{claims}".encode(), ec.ECDSA(hashes.SHA256())))
    token = f"{head}.{claims}.{b64u(r.to_bytes(32, 'big') + s.to_bytes(32, 'big'))}"
    return f"vapid t={token}, k={b64u(_raw_public(key))}"


def message(text: str, url: str = "/app") -> dict:
    """Текст сообщения бота (HTML Telegram) → заголовок и текст уведомления."""
    plain = re.sub(r"<[^>]+>", "", text).replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")
    lines = [line.strip() for line in plain.splitlines() if line.strip()]
    title = (lines[0] if lines else "Команда")[:120]
    body = " · ".join(lines[1:4])[:240]
    return {"title": title, "body": body, "url": url}


# Отправка подменяется в тестах.
async def _post(endpoint: str, body: bytes, headers: dict) -> int:
    timeout = aiohttp.ClientTimeout(total=8)
    async with aiohttp.ClientSession(timeout=timeout) as http, http.post(endpoint, data=body, headers=headers) as resp:
        return resp.status


async def send(session: AsyncSession, sub: PushSubscription, data: dict) -> bool:
    key = await vapid_key(session)
    body = encrypt(json.dumps(data, ensure_ascii=False).encode(), sub.p256dh, sub.auth)
    headers = {
        "Authorization": vapid_header(sub.endpoint, key), "Content-Encoding": "aes128gcm",
        "Content-Type": "application/octet-stream", "TTL": "86400", "Urgency": "high",
    }
    try:
        status = await _post(sub.endpoint, body, headers)
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        log.info("push failed: %s", e)
        return False
    if status in (404, 410):  # подписка больше не действует (удалили сайт, сменили браузер)
        await session.delete(sub)
        return False
    return 200 <= status < 300


async def send_to_user(session: AsyncSession, user: User, text: str, url: str = "/app") -> bool:
    subs = (await session.scalars(select(PushSubscription).where(PushSubscription.user_id == user.id))).all()
    if not subs:
        return False
    data = message(text, url)
    ok = False
    for sub in subs:  # по очереди: одна сессия БД (недействительные подписки удаляются)
        ok = await send(session, sub, data) or ok
    return ok
