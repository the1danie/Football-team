"""Адреса для Vercel: /api/webhook, /api/tick, /api/setup."""

import json
from datetime import timedelta

import pytest
from aiogram import Bot
from aiogram.methods import SendMessage, SetMyCommands, SetWebhook

from bot import webhook
from bot.config import config
from bot.db import make_engine, make_sessionmaker, normalize_db_url
from bot.models import Game, GameStatus
from bot.services import games as svc
from tests.conftest import db_url
from tests.test_bot_flow import FakeSession

SECRET = "tg-secret_123"
CRON = "cron-secret"


async def call(app, method="GET", query="", headers=None, body=b""):
    scope = {
        "type": "http", "method": method, "path": "/", "query_string": query.encode(),
        "headers": [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()],
    }
    messages = [{"type": "http.request", "body": body, "more_body": False}]
    sent = []

    async def receive():
        return messages.pop(0)

    async def send(message):
        sent.append(message)

    await app(scope, receive, send)
    return sent[0]["status"], json.loads(sent[1]["body"])


@pytest.fixture
async def fake(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "database_url", await db_url(tmp_path))
    monkeypatch.setattr(config, "webhook_secret", SECRET)
    monkeypatch.setattr(config, "cron_secret", CRON)
    monkeypatch.setattr(config, "bot_token", "42:TEST")
    monkeypatch.setattr(config, "admin_ids", [1])
    monkeypatch.setattr(config, "public_url", "")
    for name, value in (("_engine", None), ("_sessionmaker", None), ("_dp", None), ("_db_ready", False)):
        monkeypatch.setattr(webhook, name, value)
    session = FakeSession()
    monkeypatch.setattr(webhook, "make_bot", lambda: Bot("42:TEST", session=session))
    return session


def start_update(uid, text, update_id=1):
    return json.dumps({
        "update_id": update_id,
        "message": {
            "message_id": update_id, "date": 1_700_000_000, "text": text,
            "chat": {"id": uid, "type": "private"},
            "from": {"id": uid, "is_bot": False, "first_name": "Даниял"},
            "entities": [{"type": "bot_command", "offset": 0, "length": 6}] if text.startswith("/") else [],
        },
    }).encode()


async def test_webhook_rejects_bad_secret(fake):
    status, _ = await call(webhook.webhook_app, "POST", headers={"X-Telegram-Bot-Api-Secret-Token": "nope"},
                           body=start_update(1, "/start"))
    assert status == 401
    assert fake.calls == []


async def test_webhook_processes_updates_and_keeps_dialog_state(fake):
    headers = {"X-Telegram-Bot-Api-Secret-Token": SECRET}
    status, body = await call(webhook.webhook_app, "POST", headers=headers, body=start_update(1, "/start"))
    assert status == 200 and body == {"ok": True}
    assert "Как тебя зовут" in fake.sent(1)[-1].text

    # Следующий апдейт — как будто другой вызов функции: состояние диалога берётся из БД.
    webhook._dp = None
    webhook._engine = None
    await call(webhook.webhook_app, "POST", headers=headers, body=start_update(1, "Даниял", 2))
    assert "Есть ли у тебя машина" in fake.sent(1)[-1].text


async def test_webhook_survives_handler_errors(fake, monkeypatch):
    async def boom(*a, **kw):
        raise RuntimeError("boom")

    _, dp = await webhook._ensure_ready()
    monkeypatch.setattr(dp, "feed_update", boom)
    status, _ = await call(webhook.webhook_app, "POST", headers={"X-Telegram-Bot-Api-Secret-Token": SECRET},
                           body=start_update(1, "/start"))
    assert status == 200  # иначе Telegram будет повторять апдейт бесконечно


async def test_tick_auth_and_reminders(fake):
    assert (await call(webhook.tick_app))[0] == 401
    assert (await call(webhook.tick_app, query="secret=wrong"))[0] == 401

    sm, _ = await webhook._ensure_ready()
    async with sm() as s:
        user = await svc.get_or_create_user(s, 5, "Арман")
        user.has_car = True
        game = await svc.create_game(s, "game", config.now() + timedelta(hours=1), None, None)
        game.created_at -= timedelta(days=1)
        game.chat_id = -100
        await svc.set_rsvp(s, game, user, "yes")
        await s.commit()
        game_id = game.id

    status, _ = await call(webhook.tick_app, headers={"Authorization": f"Bearer {CRON}"})  # как Vercel Cron
    assert status == 200
    async with sm() as s:
        assert (await s.get(Game, game_id)).status == GameStatus.DISTRIBUTED
    assert any("Твои обязанности" in m.text for m in fake.sent(5))
    assert any("Ответственные" in m.text for m in fake.sent(-100))

    fake.reset()
    assert (await call(webhook.tick_app, query=f"secret={CRON}"))[0] == 200  # как cron-job.org
    assert [c for c in fake.calls if isinstance(c, SendMessage)] == []


async def test_setup_sets_webhook(fake):
    assert (await call(webhook.setup_app, query="secret=wrong"))[0] == 401
    status, body = await call(
        webhook.setup_app, query=f"secret={SECRET}", headers={"Host": "football.vercel.app"}
    )
    assert status == 200, body
    hook = next(c for c in fake.calls if isinstance(c, SetWebhook))
    assert hook.url == "https://football.vercel.app/api/webhook"
    assert hook.secret_token == SECRET
    assert any(isinstance(c, SetMyCommands) for c in fake.calls)
    assert body["webhook"] == hook.url and body["bot"] == "@duty_bot"


async def test_lifespan_supported():
    events = [{"type": "lifespan.startup"}, {"type": "lifespan.shutdown"}]
    sent = []

    async def receive():
        return events.pop(0)

    async def send(m):
        sent.append(m["type"])

    await webhook.webhook_app({"type": "lifespan"}, receive, send)
    assert sent == ["lifespan.startup.complete", "lifespan.shutdown.complete"]


def test_normalize_neon_url():
    url, args = normalize_db_url(
        "postgresql://user:pw@ep-cool-123-pooler.eu-central-1.aws.neon.tech/neondb?sslmode=require&channel_binding=require"
    )
    assert url == "postgresql+asyncpg://user:pw@ep-cool-123-pooler.eu-central-1.aws.neon.tech/neondb"
    assert args["ssl"] == "require" and args["statement_cache_size"] == 0
    assert normalize_db_url("postgres://u:p@h/db")[0] == "postgresql+asyncpg://u:p@h/db"
    assert normalize_db_url("sqlite+aiosqlite:///x.db") == ("sqlite+aiosqlite:///x.db", {})
    # движок создаётся без ошибок
    make_sessionmaker(make_engine("postgresql://u:p@h/db?sslmode=require", serverless=True))
