"""Сквозной сценарий через настоящий Dispatcher и фейковый Telegram API."""

import itertools
from datetime import datetime, timedelta

import pytest
from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.methods import (
    AnswerCallbackQuery,
    EditMessageReplyMarkup,
    EditMessageText,
    GetMe,
    GetWebhookInfo,
    SendMessage,
    SetWebhook,
    TelegramMethod,
)
from aiogram.types import Chat, Message, Update, User, WebhookInfo

from bot import texts
from bot.config import config
from bot.db import init_db, make_engine, make_sessionmaker
from bot.app import make_dispatcher
from bot.models import Assignment, AssignmentStatus, Game, GameStatus
from bot.scheduler import tick
from bot.services import games as svc
from tests.conftest import db_url

GROUP = -100500
ADMIN = 1
BOT_USER = User(id=999, is_bot=True, first_name="Duty", username="duty_bot")


class FakeSession(BaseSession):
    def __init__(self):
        super().__init__()
        self.calls: list[TelegramMethod] = []
        self.ids = itertools.count(100)

    async def close(self):
        pass

    async def stream_content(self, *args, **kwargs):  # pragma: no cover
        raise NotImplementedError

    async def make_request(self, bot, method, timeout=None):
        self.calls.append(method)
        if isinstance(method, GetMe):
            return BOT_USER
        if isinstance(method, SendMessage):
            chat_type = "private" if method.chat_id > 0 else "supergroup"
            return Message(
                message_id=next(self.ids), date=datetime.now(), chat=Chat(id=method.chat_id, type=chat_type),
                from_user=BOT_USER, text=method.text,
            )
        if isinstance(method, GetWebhookInfo):
            url = next((c.url for c in reversed(self.calls) if isinstance(c, SetWebhook)), "")
            return WebhookInfo(url=url, has_custom_certificate=False, pending_update_count=0)
        if isinstance(method, (EditMessageText, EditMessageReplyMarkup, AnswerCallbackQuery)):
            return True
        return True

    def sent(self, chat_id=None):
        return [c for c in self.calls if isinstance(c, SendMessage) and (chat_id is None or c.chat_id == chat_id)]

    def edits(self, chat_id=None):
        return [c for c in self.calls if isinstance(c, EditMessageText) and (chat_id is None or c.chat_id == chat_id)]

    def alerts(self):
        return [c for c in self.calls if isinstance(c, AnswerCallbackQuery) and c.text]

    def last_markup(self, chat_id):
        for c in reversed(self.calls):
            if isinstance(c, (SendMessage, EditMessageText)) and c.chat_id == chat_id and c.reply_markup:
                return c.reply_markup
        return None

    def reset(self):
        self.calls.clear()


class Harness:
    def __init__(self, dp, bot, session, sessionmaker):
        self.dp, self.bot, self.fake, self.sm = dp, bot, session, sessionmaker
        self.update_ids = itertools.count(1)
        self.msg_ids = itertools.count(10_000)

    def _user(self, uid):
        return {"id": uid, "is_bot": False, "first_name": f"U{uid}"}

    async def text(self, uid, text, chat_id=None):
        chat_id = chat_id or uid
        chat = {"id": chat_id, "type": "private" if chat_id > 0 else "supergroup"}
        upd = {
            "update_id": next(self.update_ids),
            "message": {
                "message_id": next(self.msg_ids), "date": int(datetime.now().timestamp()),
                "chat": chat, "from": self._user(uid), "text": text,
            },
        }
        if text.startswith("/"):
            upd["message"]["entities"] = [{"type": "bot_command", "offset": 0, "length": len(text.split()[0])}]
        await self.dp.feed_update(self.bot, Update.model_validate(upd, context={"bot": self.bot}))

    async def click(self, uid, data, chat_id=None, message_id=1):
        chat_id = chat_id or uid
        chat = {"id": chat_id, "type": "private" if chat_id > 0 else "supergroup"}
        upd = {
            "update_id": next(self.update_ids),
            "callback_query": {
                "id": str(next(self.update_ids)), "from": self._user(uid), "chat_instance": "x", "data": data,
                "message": {"message_id": message_id, "date": int(datetime.now().timestamp()), "chat": chat,
                            "from": BOT_USER.model_dump(), "text": "..."},
            },
        }
        await self.dp.feed_update(self.bot, Update.model_validate(upd, context={"bot": self.bot}))

    async def register(self, uid, name, car):
        await self.text(uid, "/start")
        await self.text(uid, name)
        await self.click(uid, f"car:{1 if car else 0}")


def buttons(markup):
    return [b for row in markup.inline_keyboard for b in row]


@pytest.fixture
async def h(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "admin_ids", [ADMIN])
    monkeypatch.setattr(config, "group_chat_id", None)
    engine = make_engine(await db_url(tmp_path))
    await init_db(engine)
    sm = make_sessionmaker(engine)
    fake = FakeSession()
    bot = Bot("42:TEST", session=fake)
    dp = make_dispatcher(sm)  # тот же диспетчер, что в проде, включая хранение диалогов в БД
    yield Harness(dp, bot, fake, sm)
    await engine.dispose()


async def test_full_flow(h: Harness):
    fake = h.fake

    # --- регистрация
    await h.register(ADMIN, "Даниял", car=True)
    assert "Привет" not in fake.sent(ADMIN)[-1].text  # после профиля — меню с помощью
    players = {2: ("Арман", False), 3: ("Тимур", True), 4: ("Руслан", False), 5: ("Максим", False)}
    for uid, (name, car) in players.items():
        await h.register(uid, name, car)

    # --- создание игры без привязанного чата
    fake.reset()
    await h.text(ADMIN, texts.BTN_CREATE)
    assert "/bindchat" in fake.sent(ADMIN)[-1].text

    # не-админ не может привязать чат
    await h.text(2, "/bindchat", chat_id=GROUP)
    assert "только для администраторов" in fake.sent(GROUP)[-1].text
    await h.text(ADMIN, "/bindchat", chat_id=GROUP)
    assert "привязан" in fake.sent(GROUP)[-1].text

    # --- создание игры: дата, время, тип, место
    fake.reset()
    await h.text(ADMIN, texts.BTN_CREATE)
    tomorrow = config.now() + timedelta(days=1)
    await h.click(ADMIN, f"newdate:{tomorrow:%Y-%m-%d}")
    await h.text(ADMIN, "20:00")
    await h.click(ADMIN, "newkind:game")
    await h.text(ADMIN, "Стадион Динамо")
    assert "Проверьте" in fake.sent(ADMIN)[-1].text
    await h.click(ADMIN, "newgame:publish")
    announce = fake.sent(GROUP)[-1]
    assert "⚽ Игра" in announce.text and "🕗 20:00" in announce.text and "Стадион Динамо" in announce.text
    assert [b.text for b in buttons(announce.reply_markup)] == ["✅ Буду", "❌ Не буду", "🤔 Пока не знаю"]
    game_id = int(buttons(announce.reply_markup)[0].callback_data.split(":")[1])

    # --- отметки в группе (6 — незнакомый игрок, профиль создаётся автоматически)
    fake.reset()
    for uid in (ADMIN, 2, 3, 4, 5):
        await h.click(uid, f"rsvp:{game_id}:yes", chat_id=GROUP)
    await h.click(6, f"rsvp:{game_id}:maybe", chat_id=GROUP)
    assert "Заполни профиль" in fake.alerts()[-1].text
    last_edit = fake.edits(GROUP)[-1]
    assert "Подтвердили: 5 человек" in last_edit.text
    assert "🤔 Пока не знаю (1): U6" in last_edit.text

    # повторное нажатие
    await h.click(2, f"rsvp:{game_id}:yes", chat_id=GROUP)
    assert "уже отметил" in fake.alerts()[-1].text

    # --- участники и распределение
    fake.reset()
    await h.text(ADMIN, texts.BTN_PARTICIPANTS)
    assert "✅ Буду — 5" in fake.sent(ADMIN)[-1].text
    await h.text(ADMIN, texts.BTN_DISTRIBUTE)
    summary = next(m for m in fake.sent(GROUP) if "Обязанности" in m.text)
    assert "Участников: 5" in summary.text
    assert buttons(summary.reply_markup)[0].url == f"https://t.me/duty_bot?start=swap_{game_id}"
    for duty in ("⚽ Мячи", "💧 Вода", "👕 Манишки", "🧺 Стирка манишек"):
        assert duty in summary.text
    async with h.sm() as s:
        active = await svc.active_assignments(s, game_id)
    assert len(active) == 4 and len({a.user_id for a in active}) == 4
    balls = next(a for a in active if a.duty.code == "balls")
    assert balls.user.has_car
    # каждому назначенному пришло личное сообщение
    assert len([m for m in fake.sent() if "Тебе назначена обязанность" in m.text]) == 4

    # повторное распределение требует подтверждения
    fake.reset()
    await h.text(ADMIN, texts.BTN_DISTRIBUTE)
    assert "Пересчитать заново" in fake.sent(ADMIN)[-1].text

    # --- водитель с мячами отказывается — мячи переходят другому водителю
    fake.reset()
    driver_tg = balls.user.telegram_id
    await h.click(driver_tg, f"rsvp:{game_id}:no", chat_id=GROUP)
    alert = fake.alerts()[-1].text
    assert "У тебя были назначены ⚽ мячи" in alert and "передана другому игроку" in alert
    async with h.sm() as s:
        active = await svc.active_assignments(s, game_id)
    new_balls = next(a for a in active if a.duty.code == "balls")
    assert new_balls.user.telegram_id not in (driver_tg,) and new_balls.user.has_car
    assert driver_tg not in {a.user.telegram_id for a in active}
    assert len(active) == 4

    # --- обмен: игрок без машины меняется с другим без машины
    async with h.sm() as s:
        active = await svc.active_assignments(s, game_id)
    walkers = [a for a in active if not a.user.has_car]
    a1, a2 = walkers[0], walkers[1]
    fake.reset()
    await h.text(a1.user.telegram_id, f"/start swap_{game_id}")
    offer_menu = fake.sent(a1.user.telegram_id)[-1]
    assert f"Твоя обязанность: <b>{a1.duty.title}</b>" in offer_menu.text
    labels = [b.text for b in buttons(offer_menu.reply_markup)]
    assert f"{a2.user.name} — {a2.duty.title}" in labels
    assert not any(texts.h(new_balls.user.name) in l and "Мячи" in l for l in labels)  # мячи без машины не взять

    await h.click(a1.user.telegram_id, f"swapto:{a1.id}:{a2.user.id}")
    offer = fake.sent(a2.user.telegram_id)[-1]
    assert "предлагает обмен" in offer.text
    accept = buttons(offer.reply_markup)[0].callback_data

    # чужой не может принять
    await h.click(a1.user.telegram_id, accept)
    assert "не для тебя" in fake.alerts()[-1].text

    await h.click(a2.user.telegram_id, accept)
    assert "согласился на обмен" in fake.sent(a1.user.telegram_id)[-1].text
    async with h.sm() as s:
        swapped = {a.duty_id: a.user_id for a in await svc.active_assignments(s, game_id)}
    assert swapped[a1.duty_id] == a2.user_id and swapped[a2.duty_id] == a1.user_id

    # --- ручное изменение назначения
    fake.reset()
    await h.click(ADMIN, f"edset:{game_id}:{a1.duty_id}:{a1.user_id}")
    assert fake.edits(ADMIN)[-1].text == f"✅ {a1.duty.title} — {a1.user.name}"
    # у a1 теперь две обязанности, у a2 — ни одной
    async with h.sm() as s:
        assert len(await svc.user_assignments(s, game_id, a1.user_id)) == 2
        assert await svc.user_assignments(s, game_id, a2.user_id) == []
    assert any("снял с тебя обязанность" in m.text for m in fake.sent(a2.user.telegram_id))

    # --- статистика
    fake.reset()
    await h.text(2, texts.BTN_STATS)
    stat = fake.sent(2)[-1]
    assert "Статистика команды" in stat.text and "обязанност" in stat.text
    await h.click(2, f"stat:{a1.user_id}")
    assert "💧 Вода" in fake.edits(2)[-1].text

    # --- напоминания: личное и в общий чат
    fake.reset()
    async with h.sm() as s:
        game = await s.get(Game, game_id)
        game.starts_at = config.now() + timedelta(hours=1)
        await s.commit()
    async with h.sm() as s:
        await tick(h.bot, s)
        await s.commit()
    personal = [m for m in fake.sent() if m.chat_id > 0 and ("Твоя обязанность" in m.text or "Твои обязанности" in m.text)]
    async with h.sm() as s:
        holders = {a.user_id for a in await svc.active_assignments(s, game_id)}
    assert len(personal) == len(holders)
    assert any("Твои обязанности" in m.text for m in fake.sent(a1.user.telegram_id))
    assert any("привезти мячи" in m.text for m in personal)
    group = fake.sent(GROUP)[-1].text
    assert "Сегодня игра в" in group and "Ответственные" in group
    # повторно не шлём
    fake.reset()
    async with h.sm() as s:
        await tick(h.bot, s)
        await s.commit()
    assert fake.sent() == []

    # --- отмена игры
    fake.reset()
    await h.click(ADMIN, f"adm:cancelyes:{game_id}")
    assert "отменена" in fake.sent(GROUP)[-1].text
    async with h.sm() as s:
        game = await s.get(Game, game_id)
        assert game.status == GameStatus.CANCELLED
        assert await svc.active_assignments(s, game_id) == []
    # отметиться в отменённую игру нельзя
    await h.click(2, f"rsvp:{game_id}:yes", chat_id=GROUP)
    assert "закрыт" in fake.alerts()[-1].text


async def test_auto_distribution_and_finish(h: Harness):
    await h.register(ADMIN, "Даниял", car=True)
    await h.text(ADMIN, "/bindchat", chat_id=GROUP)
    async with h.sm() as s:
        game = await svc.create_game(s, "training", config.now() + timedelta(days=2), None, None)
        game.chat_id = GROUP
        admin = await svc.get_user_by_tg(s, ADMIN)
        await svc.set_rsvp(s, game, admin, "yes")
        await s.commit()
        game_id = game.id

    # Время распределения не наступило
    async with h.sm() as s:
        await tick(h.bot, s)
        await s.commit()
        assert (await s.get(Game, game_id)).status == GameStatus.OPEN

    # Сдвигаем игру ближе — распределение автоматически
    h.fake.reset()
    async with h.sm() as s:
        g = await s.get(Game, game_id)
        g.starts_at = config.now() + timedelta(hours=4)
        g.created_at = g.created_at - timedelta(days=2)  # создана заранее
        await s.commit()
    async with h.sm() as s:
        await tick(h.bot, s)
        await s.commit()
        assert (await s.get(Game, game_id)).status == GameStatus.DISTRIBUTED
    assert any("Автоматическое распределение" in m.text for m in h.fake.sent(ADMIN))
    # один игрок — все четыре обязанности на нём
    async with h.sm() as s:
        assert len(await svc.user_assignments(s, game_id, (await svc.get_user_by_tg(s, ADMIN)).id)) == 4

    # Игра прошла — завершается
    async with h.sm() as s:
        g = await s.get(Game, game_id)
        g.starts_at = config.now() - timedelta(hours=5)
        await s.commit()
    async with h.sm() as s:
        await tick(h.bot, s)
        await s.commit()
        assert (await s.get(Game, game_id)).status == GameStatus.FINISHED
        assert all(a.status == AssignmentStatus.ACTIVE for a in (await s.scalars(
            __import__("sqlalchemy").select(Assignment).where(Assignment.game_id == game_id))).all())
