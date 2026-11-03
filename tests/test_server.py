"""Свой сервер (bot/server.py): те же адреса, что на Vercel, и планировщик внутри."""

import asyncio
import json

from bot import server
from bot.config import config
from tests.test_webhook import CRON, SECRET, fake, start_update  # noqa: F401 — фикстура fake


async def request(path, method="GET", query="", headers=None, body=b""):
    scope = {
        "type": "http", "method": method, "path": path, "query_string": query.encode(),
        "headers": [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()],
    }
    messages = [{"type": "http.request", "body": body, "more_body": False}]
    sent = []

    async def receive():
        return messages.pop(0)

    async def send(message):
        sent.append(message)

    await server.app(scope, receive, send)
    return sent[0]["status"], dict(sent[0]["headers"]), sent[1]["body"]


async def test_routes(fake):  # noqa: F811
    for path in ("/", "/app", "/api/app"):
        status, headers, body = await request(path)
        assert status == 200 and headers[b"content-type"].startswith(b"text/html") and b"<html" in body.lower()
    status, headers, _ = await request("/sw.js")
    assert status == 200 and b"javascript" in headers[b"content-type"]
    status, _, body = await request("/api/health")
    assert status == 200 and json.loads(body)["ok"] in (True, False)
    status, _, body = await request("/api/miniapp", "POST", body=b'{"action": "state"}')
    assert status == 401
    status, _, _ = await request("/api/tick")
    assert status == 401  # без секрета
    status, _, _ = await request("/api/webhook", "POST", body=b"{}")
    assert status == 401  # без секрета Telegram
    status, _, _ = await request("/nope")
    assert status == 404


async def test_webhook_update_through_server(fake):  # noqa: F811
    status, _, _ = await request("/api/webhook", "POST", headers={"X-Telegram-Bot-Api-Secret-Token": SECRET},
                                 body=start_update(1, "/start"))
    assert status == 200
    assert "Привет, Даниял" in fake.sent(1)[-1].text


async def test_silent_until_takeover(fake, monkeypatch):  # noqa: F811
    """До переноса базы сервер не трогает Telegram (webhook остаётся на Vercel)."""
    from aiogram.methods import SetWebhook

    monkeypatch.setattr(config, "public_url", "https://1-2-3-4.sslip.io")
    monkeypatch.setattr(config, "manage_webhook", False)
    await server._check_webhook(server.webhook.make_bot())
    assert not any(isinstance(c, SetWebhook) for c in fake.calls)
    monkeypatch.setattr(config, "manage_webhook", True)
    await server._check_webhook(server.webhook.make_bot())
    assert fake.webhook_url == "https://1-2-3-4.sslip.io/api/webhook"


async def test_lifespan_starts_scheduler(fake, monkeypatch):  # noqa: F811
    ticks = []

    async def fake_tick(bot, session):
        ticks.append(1)

    monkeypatch.setattr(server, "tick", fake_tick)
    monkeypatch.setattr(server, "TICK_SECONDS", 0.01)
    monkeypatch.setattr(config, "manage_webhook", False)
    events = [{"type": "lifespan.startup"}]
    sent = []
    done = asyncio.Event()

    async def receive():
        if events:
            return events.pop(0)
        await done.wait()
        return {"type": "lifespan.shutdown"}

    async def send(m):
        sent.append(m["type"])

    task = asyncio.create_task(server.app({"type": "lifespan"}, receive, send))
    for _ in range(100):
        if len(ticks) >= 2:
            break
        await asyncio.sleep(0.02)
    done.set()
    await task
    assert sent == ["lifespan.startup.complete", "lifespan.shutdown.complete"] and len(ticks) >= 2
