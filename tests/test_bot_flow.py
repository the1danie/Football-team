"""Сквозной сценарий через настоящий Dispatcher и фейковый Telegram API."""

import itertools
from datetime import datetime, timedelta

import pytest
from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.methods import (
    AnswerCallbackQuery,
    DeleteWebhook,
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
        self.webhook_url = ""

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
        if isinstance(method, SetWebhook):
            self.webhook_url = method.url
        if isinstance(method, DeleteWebhook):
            self.webhook_url = ""
        if isinstance(method, GetWebhookInfo):
            return WebhookInfo(url=self.webhook_url, has_custom_certificate=False, pending_update_count=0)
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
        self.names: dict[int, str] = {}  # имя в профиле Telegram

    def _user(self, uid):
        return {"id": uid, "is_bot": False, "first_name": self.names.get(uid, f"U{uid}")}

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

    async def register(self, uid, name, car, approve=True):
        self.names[uid] = name  # имя бот берёт из профиля Telegram
        await self.text(uid, "/start")
        await self.click(uid, f"car:{1 if car else 0}")
        if approve and uid != ADMIN:  # админ принимает заявку
            async with self.sm() as s:
                user = await svc.get_user_by_tg(s, uid)
            await self.click(ADMIN, f"pl:approve:{user.id}")


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

    # --- регистрация: имя из профиля Telegram, спрашивается только машина
    h.names[ADMIN] = "Даниял"
    await h.text(ADMIN, "/start")
    first = fake.sent(ADMIN)[-1].text
    assert "Привет, Даниял" in first and "Есть ли у тебя машина" in first and "Как тебя зовут" not in first
    await h.click(ADMIN, "car:1")
    assert "Привет" not in fake.sent(ADMIN)[-1].text  # после профиля — меню с помощью
    players = {2: ("Арман", False), 3: ("Тимур", True), 4: ("Руслан", False), 5: ("Максим", False)}
    for uid, (name, car) in players.items():
        await h.register(uid, name, car)

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

    # --- отметки в группе (6 — посторонний: не зарегистрирован и не подтверждён — не пускаем)
    fake.reset()
    for uid in (ADMIN, 2, 3, 4, 5):
        await h.click(uid, f"rsvp:{game_id}:yes", chat_id=GROUP)
    await h.click(6, f"rsvp:{game_id}:maybe", chat_id=GROUP)
    assert "нажми /start" in fake.alerts()[-1].text
    last_edit = fake.edits(GROUP)[-1]
    assert "Подтвердили: 5 человек" in last_edit.text
    assert "🤔 Пока не знаю (0)" in last_edit.text

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
    assert "Рейтинг" in stat.text and "обязанност" in stat.text
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
    assert any("мячи" in m.text for m in personal)
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


async def _tick(h: Harness):
    async with h.sm() as s:
        await tick(h.bot, s)
        await s.commit()


async def _move_game(h: Harness, game_id: int, starts_in: timedelta):
    async with h.sm() as s:
        g = await s.get(Game, game_id)
        g.starts_at = config.now() + starts_in
        g.created_at = g.created_at - timedelta(days=2)
        # игроки были в боте до публикации игры
        for u in (await s.scalars(__import__("sqlalchemy").select(svc.User))).all():
            u.created_at = min(u.created_at, g.created_at - timedelta(minutes=1))
        await s.commit()


async def test_poll_nudge_and_penalties(h: Harness, monkeypatch):
    monkeypatch.setattr(config, "penalty_limit", 1)
    fake = h.fake
    await h.register(ADMIN, "Даниял", car=True)
    for uid, name in ((2, "Арман"), (3, "Тимур"), (4, "Руслан")):
        await h.register(uid, name, car=False)
    await h.text(ADMIN, "/bindchat", chat_id=GROUP)

    # Тимур травмирован — админ убирает его из состава
    async with h.sm() as s:
        timur = await svc.get_user_by_tg(s, 3)
        ruslan = await svc.get_user_by_tg(s, 4)
    await h.text(ADMIN, "/players")
    assert "Игроки" in fake.sent(ADMIN)[-1].text
    await h.click(ADMIN, f"pl:active:{timur.id}")
    await h.click(ADMIN, "pl:list:0")
    assert "🚫 Тимур" in [b.text for b in buttons(fake.last_markup(ADMIN))]

    # --- публикация: опрос приходит в личку всем из состава, кроме автора
    fake.reset()
    await h.text(ADMIN, texts.BTN_CREATE)
    tomorrow = config.now() + timedelta(days=1)
    await h.click(ADMIN, f"newdate:{tomorrow:%Y-%m-%d}")
    await h.click(ADMIN, "newtime:1200")
    await h.click(ADMIN, "newkind:game")
    await h.click(ADMIN, "newloc:skip")
    await h.click(ADMIN, "newgame:publish")
    invites = [m for m in fake.sent() if "Открыт сбор" in m.text]
    assert sorted(m.chat_id for m in invites) == [2, 4]
    assert "получит 1 минус" in invites[0].text
    game_id = int(buttons(invites[0].reply_markup)[0].callback_data.split(":")[1])
    report = next(m.text for m in fake.sent(ADMIN) if "Опрос отправлен" in m.text)
    assert "Опрос отправлен в личку: 2" in report and "Сбор закрывается" in report

    # Арман отвечает прямо из личного сообщения
    fake.reset()
    await h.click(2, f"rsvp:{game_id}:yes")
    assert "Твой статус: ✅ Буду" in fake.edits(2)[-1].text
    assert "Подтвердили: 1 человек" in fake.edits(GROUP)[-1].text

    # --- напоминание молчащим: лично и списком в чате, один раз
    await _move_game(h, game_id, timedelta(hours=7))  # сбор закроется через 2 ч, напоминание — за 3 ч
    fake.reset()
    await _tick(h)
    nudges = [m for m in fake.sent() if "ещё не ответил" in m.text]
    assert sorted(m.chat_id for m in nudges) == [ADMIN, 4]  # автор игры тоже не отметился
    group = fake.sent(GROUP)[-1].text
    assert "Ещё не отметились (2)" in group and "Руслан" in group and "Арман" not in group and "Тимур" not in group
    fake.reset()
    await _tick(h)
    assert fake.sent() == []

    # --- сбор закрыт: минус Руслану (и автору игры, который сам не отметился)
    await _move_game(h, game_id, timedelta(hours=4))
    fake.reset()
    await _tick(h)
    penalized = sorted(m.chat_id for m in fake.sent() if "Ты не ответил на опрос" in m.text)
    assert penalized == [ADMIN, 4]
    assert any("Начислено: −1. Всего: 1 минус." in m.text for m in fake.sent(4))
    assert any("Не ответили на опрос" in m.text and "Руслан" in m.text for m in fake.sent(GROUP))
    admin_texts = [m.text for m in fake.sent(ADMIN)]
    assert any("1 и больше минусов" in t for t in admin_texts)  # лимит
    assert any("Автоматическое распределение" in t for t in admin_texts)
    async with h.sm() as s:
        assert (await svc.open_penalty_points(s)) == {ruslan.id: 1, (await svc.get_user_by_tg(s, ADMIN)).id: 1}
    # повторный тик не начисляет ещё раз
    fake.reset()
    await _tick(h)
    assert not any("Ты не ответил" in m.text for m in fake.sent())

    # --- минусы видны в профиле и статистике
    fake.reset()
    await h.text(4, texts.BTN_PROFILE)
    assert "Минусы: 1" in fake.sent(4)[-1].text
    await h.text(4, texts.BTN_STATS)
    assert "Руслан — 0 обязанностей · ⚠️ −1" in fake.sent(4)[-1].text

    # --- игрок не может смотреть /penalties, админ — может и снимает минус
    fake.reset()
    await h.text(4, "/penalties")
    assert not any("Минусы за неответы" in m.text for m in fake.sent(4))
    await h.text(ADMIN, "/penalties")
    assert "Руслан — 1 минус" in fake.sent(ADMIN)[-1].text
    await h.click(ADMIN, f"pens:{ruslan.id}")
    del_button = next(b for b in buttons(fake.last_markup(ADMIN)) if b.callback_data.startswith("pendel:"))
    await h.click(ADMIN, del_button.callback_data)
    assert "снял с тебя минус" in fake.sent(4)[-1].text
    await h.click(ADMIN, del_button.callback_data)
    assert "уже снят" in fake.alerts()[-1].text
    async with h.sm() as s:
        assert ruslan.id not in await svc.open_penalty_points(s)


async def test_no_penalties_for_last_minute_game(h: Harness):
    await h.register(ADMIN, "Даниял", car=True)
    await h.register(2, "Арман", car=False)
    async with h.sm() as s:
        game = await svc.create_game(s, "game", config.now() + timedelta(hours=2), None, None)
        await s.commit()
        game_id = game.id
    await _tick(h)
    async with h.sm() as s:
        g = await s.get(Game, game_id)
        assert g.penalties_applied  # сбор фактически закрыт, но ответить было некогда
        assert await svc.open_penalty_points(s) == {}


async def test_name_from_telegram(h: Harness):
    h.names[2] = "Арман"
    await h.register(2, "Арман", car=False)
    # второй Арман — бот различает их по фамилии
    upd = {"id": 3, "is_bot": False, "first_name": "Арман", "last_name": "Сейткали"}
    h._user = lambda uid, _orig=h._user: upd if uid == 3 else _orig(uid)
    await h.text(3, "/start")
    assert "Привет, Арман Сейткали" in h.fake.sent(3)[-1].text
    await h.click(3, "car:0")
    async with h.sm() as s:
        await h.click(ADMIN, f"pl:approve:{(await svc.get_user_by_tg(s, 3)).id}")
    # имя можно поменять в профиле
    await h.click(3, "prof:name")
    await h.text(3, "Арман С.")
    async with h.sm() as s:
        assert (await svc.get_user_by_tg(s, 3)).name == "Арман С."
        assert (await svc.get_user_by_tg(s, 2)).name == "Арман"


def _wa_text(msg) -> str:
    """Текст, который откроется в WhatsApp по кнопке под сообщением."""
    from urllib.parse import unquote

    url = buttons(msg.reply_markup)[0].url
    assert url.startswith("https://wa.me/?text=")
    return unquote(url.removeprefix("https://wa.me/?text="))


async def test_whatsapp_team_without_telegram_group(h: Harness):
    """Команда сидит в WhatsApp: Telegram-группы нет, админ получает готовые тексты для WhatsApp."""
    fake = h.fake
    await h.register(ADMIN, "Даниял", car=True)
    await h.register(2, "Арман", car=False)

    # --- игра создаётся без /bindchat
    fake.reset()
    await h.text(ADMIN, texts.BTN_CREATE)
    tomorrow = config.now() + timedelta(days=1)
    await h.click(ADMIN, f"newdate:{tomorrow:%Y-%m-%d}")
    await h.click(ADMIN, "newtime:1200")
    await h.click(ADMIN, "newkind:game")
    await h.text(ADMIN, "Стадион Динамо")
    await h.click(ADMIN, "newgame:publish")
    assert not [m for m in fake.sent() if m.chat_id < 0]  # в Telegram-группы ничего
    draft = fake.sent(ADMIN)[-1]
    assert draft.parse_mode is None  # обычный текст — копируется как есть
    wa = _wa_text(draft)
    assert wa == draft.text
    assert "*Игра —" in wa and "Стадион Динамо" in wa and "Кто не ответит — получит минус" in wa
    link = next(line for line in wa.splitlines() if line.startswith("https://t.me/"))
    game_id = int(link.rsplit("_", 1)[1])
    assert link == f"https://t.me/duty_bot?start=game_{game_id}"
    assert any(m.chat_id == 2 and "Открыт сбор" in m.text for m in fake.sent())  # опрос в личку

    # --- новый игрок пришёл по ссылке из WhatsApp: регистрация → админ принимает → сразу опрос
    fake.reset()
    h.names[5] = "Максим"
    await h.text(5, f"/start game_{game_id}")
    assert "Есть ли у тебя машина" in fake.sent(5)[-1].text
    await h.click(5, "car:0")
    assert "Заявка отправлена" in fake.edits(5)[-1].text
    async with h.sm() as s:
        await h.click(ADMIN, f"pl:approve:{(await svc.get_user_by_tg(s, 5)).id}")
    card = fake.sent(5)[-1]
    assert "Игра —" in card.text and "Твой статус: не отмечен" in card.text
    assert [b.text for b in buttons(card.reply_markup)][:3] == ["✅ Буду", "❌ Не буду", "🤔 Пока не знаю"]
    await h.click(5, f"rsvp:{game_id}:yes")
    assert "Твой статус: ✅ Буду" in fake.edits(5)[-1].text

    # --- админ в любой момент берёт текущий список для WhatsApp
    fake.reset()
    await h.click(ADMIN, f"wa:{game_id}")
    assert "Буду (1): Максим" in _wa_text(fake.sent(ADMIN)[-1])

    # --- напоминание молчащим: лично + текст для WhatsApp админу
    async with h.sm() as s:
        g = await s.get(Game, game_id)
        g.created_at -= timedelta(days=2)
        for u in (await s.scalars(__import__("sqlalchemy").select(svc.User))).all():
            u.created_at -= timedelta(days=3)
        g.starts_at = config.now() + timedelta(hours=7)
        await s.commit()
    fake.reset()
    await _tick(h)
    wa = _wa_text(fake.sent(ADMIN)[-1])
    assert "Ещё не отметились (2): Арман, Даниял" in wa and link in wa and "потом минус" in wa

    # --- закрытие сбора: минусы и обязанности — тоже текстами для WhatsApp
    async with h.sm() as s:
        (await s.get(Game, game_id)).starts_at = config.now() + timedelta(hours=4)
        await s.commit()
    fake.reset()
    await _tick(h)
    drafts = [_wa_text(m) for m in fake.sent(ADMIN) if m.reply_markup and buttons(m.reply_markup)[0].url]
    assert any("Не ответили на опрос: Арман, Даниял — по −1" in d for d in drafts)
    duties = next(d for d in drafts if "*Обязанности*" in d)
    assert "Вода — Максим" in duties or "Максим" in duties
    assert "Мячи — не назначено" in duties  # у Максима нет машины
    assert not [m for m in fake.sent() if m.chat_id < 0]

    # --- напоминание перед игрой
    async with h.sm() as s:
        (await s.get(Game, game_id)).starts_at = config.now() + timedelta(hours=1)
        await s.commit()
    fake.reset()
    await _tick(h)
    reminder = _wa_text(fake.sent(ADMIN)[-1])
    assert "*Сегодня игра в" in reminder and "Ответственные:" in reminder and "Максим" in reminder

    # --- отмена
    fake.reset()
    await h.click(ADMIN, f"adm:cancelyes:{game_id}")
    assert any("отменена" in _wa_text(m) for m in fake.sent(ADMIN) if m.reply_markup)


def test_whatsapp_share_url_limit():
    from bot import whatsapp

    assert whatsapp.share_markup("Привет") is not None
    assert whatsapp.share_markup("Я" * 2000) is None  # слишком длинно для кнопки — только текст


async def test_whatsapp_draft_falls_back_to_plain_text(monkeypatch):
    from aiogram.exceptions import TelegramBadRequest

    from bot import whatsapp

    session = FakeSession()
    original = session.make_request

    async def picky(bot, method, timeout=None):
        if isinstance(method, SendMessage) and method.reply_markup is not None:
            raise TelegramBadRequest(method=method, message="BUTTON_URL_INVALID")
        return await original(bot, method, timeout)

    session.make_request = picky
    bot = Bot("42:TEST", session=session)
    await whatsapp.send_draft(bot, "Текст", chat_ids=[1])
    assert [m.text for m in session.sent(1)] == ["Текст"]



def test_time_choice_and_parsing():
    from bot.handlers.admin import parse_time
    from bot.keyboards import time_choice

    labels = [b.text for row in time_choice().inline_keyboard for b in row]
    assert labels[0] == "13:00" and labels[1] == "13:30" and labels[-2] == "23:30" and labels[-1] == "24:00"
    assert len(labels) == 23
    assert parse_time("20:30") == 20 * 60 + 30
    assert parse_time("24:00") == 24 * 60
    assert parse_time("24:30") is None and parse_time("19:75") is None



async def test_new_players_need_admin_approval(h: Harness):
    fake = h.fake
    await h.register(ADMIN, "Даниял", car=True)

    # --- незнакомец регистрируется: заявка админу, доступа пока нет
    fake.reset()
    h.names[7] = "Незнакомец"
    await h.text(7, "/start")
    await h.click(7, "car:0")
    assert "Заявка отправлена администратору" in fake.edits(7)[-1].text
    request = next(m for m in fake.sent(ADMIN) if "Новый игрок" in m.text)
    assert "Незнакомец" in request.text
    labels = [b.text for b in buttons(request.reply_markup)]
    assert "✅ Принять в команду" in labels and "⛔ Отклонить" in labels

    await h.text(7, texts.BTN_STATS)
    assert "Заявка у администратора" in fake.sent(7)[-1].text
    async with h.sm() as s:
        game = await svc.create_game(s, "game", config.now() + timedelta(days=1), None, None)
        await s.commit()
        game_id = game.id
        stranger = await svc.get_user_by_tg(s, 7)
    await h.click(7, f"rsvp:{game_id}:yes")
    assert "Заявка у администратора" in fake.alerts()[-1].text
    async with h.sm() as s:
        assert await svc.get_rsvp(s, game_id, stranger.id) is None
        assert stranger.id not in {u.id for u in await svc.roster(s)}

    # --- незарегистрированный вообще
    await h.click(8, f"rsvp:{game_id}:yes", chat_id=GROUP)
    assert "нажми /start" in fake.alerts()[-1].text

    # --- админ отклоняет: доступ закрыт
    fake.reset()
    await h.click(ADMIN, f"pl:block:{stranger.id}")
    assert "Заявка отклонена" in fake.sent(7)[-1].text
    await h.text(7, "/start")
    assert "Доступ к боту закрыт" in fake.sent(7)[-1].text

    # --- передумал и принял: игроку приходит приветствие и открытая игра
    fake.reset()
    await h.click(ADMIN, f"pl:approve:{stranger.id}")
    assert any("добавил тебя в команду" in m.text for m in fake.sent(7))
    assert any("Игра —" in m.text and m.reply_markup for m in fake.sent(7))
    await h.click(7, f"rsvp:{game_id}:yes")
    async with h.sm() as s:
        assert await svc.get_rsvp(s, game_id, stranger.id) == "yes"


async def test_admin_controls_car(h: Harness):
    fake = h.fake
    await h.register(ADMIN, "Даниял", car=False)
    await h.register(2, "Арман", car=False)  # «забыл» указать машину
    async with h.sm() as s:
        arman = await svc.get_user_by_tg(s, 2)

    # игрок сам меняет машину — админ получает уведомление
    fake.reset()
    await h.click(2, "pcar:1")
    assert any("Арман изменил в профиле" in m.text for m in fake.sent(ADMIN))
    await h.click(2, "pcar:0")

    # админ ставит машину — закреплено, игрок изменить не может
    fake.reset()
    await h.text(ADMIN, texts.BTN_PLAYERS)
    assert any(b.callback_data == f"pl:show:{arman.id}" for b in buttons(fake.sent(ADMIN)[-1].reply_markup))
    await h.click(ADMIN, f"pl:show:{arman.id}")
    await h.click(ADMIN, f"pl:car:{arman.id}")
    assert "закреплено админом" in fake.edits(ADMIN)[-1].text
    assert "есть машина" in fake.sent(2)[-1].text.lower()
    await h.click(2, "prof:car")
    assert "изменить может только он" in fake.alerts()[-1].text
    await h.click(2, "pcar:0")
    assert "изменить может только он" in fake.alerts()[-1].text
    async with h.sm() as s:
        assert (await svc.get_user_by_tg(s, 2)).has_car is True

    # при повторной регистрации машина тоже не меняется
    await h.text(2, "/start")

    # разблокировать и переименовать
    await h.click(ADMIN, f"pl:unlock:{arman.id}")
    await h.click(ADMIN, f"pl:name:{arman.id}")
    await h.text(ADMIN, "Арман К.")
    async with h.sm() as s:
        u = await svc.get_user_by_tg(s, 2)
        assert u.name == "Арман К." and u.car_locked is False

    # временно убрать из состава и вернуть
    await h.click(ADMIN, f"pl:active:{arman.id}")
    async with h.sm() as s:
        assert arman.id not in {u.id for u in await svc.roster(s)}
    await h.click(ADMIN, f"pl:active:{arman.id}")
    async with h.sm() as s:
        assert arman.id in {u.id for u in await svc.roster(s)}


async def test_blocking_player_frees_duties(h: Harness):
    await h.register(ADMIN, "Даниял", car=True)
    await h.register(2, "Арман", car=True)
    async with h.sm() as s:
        game = await svc.create_game(s, "game", config.now() + timedelta(days=1), None, None)
        for tg in (ADMIN, 2):
            await svc.set_rsvp(s, game, await svc.get_user_by_tg(s, tg), "yes")
        await svc.distribute_game(s, game)
        await s.commit()
        game_id, arman = game.id, await svc.get_user_by_tg(s, 2)
    await h.click(ADMIN, f"pl:block:{arman.id}")
    async with h.sm() as s:
        assert await svc.get_rsvp(s, game_id, arman.id) == "no"
        assert {a.user_id for a in await svc.active_assignments(s, game_id)} == {
            (await svc.get_user_by_tg(s, ADMIN)).id
        }


async def test_owner_delegates_admin_rights(h: Harness):
    fake = h.fake
    await h.register(ADMIN, "Даниял", car=True)  # главный админ (ADMIN_IDS)
    await h.register(2, "Арман", car=False)
    await h.register(3, "Тимур", car=False)
    async with h.sm() as s:
        arman = await svc.get_user_by_tg(s, 2)
        timur = await svc.get_user_by_tg(s, 3)
        owner = await svc.get_user_by_tg(s, ADMIN)

    # до выдачи прав Арман — обычный игрок
    fake.reset()
    await h.text(2, "/players")
    assert not any("Игроки" in m.text for m in fake.sent(2))

    # главный видит кнопку и выдаёт права
    await h.click(ADMIN, f"pl:show:{arman.id}")
    labels = [b.text for b in buttons(fake.last_markup(ADMIN))]
    assert "👑 Сделать админом" in labels
    await h.click(ADMIN, f"pl:admin_on:{arman.id}")
    assert "👑 админ" in fake.edits(ADMIN)[-1].text
    assert any("выдали права администратора" in m.text for m in fake.sent(2))

    # теперь Арман — админ: меню, игроки, создание игры
    fake.reset()
    await h.text(2, "/players")
    assert "Игроки" in fake.sent(2)[-1].text
    await h.click(2, f"pl:show:{timur.id}")
    assert "👑 Сделать админом" not in [b.text for b in buttons(fake.last_markup(2))]  # раздаёт только главный
    await h.click(2, f"pl:admin_on:{timur.id}")
    assert "только главный админ" in fake.alerts()[-1].text
    await h.click(2, f"pl:block:{owner.id}")
    assert "Главного админа нельзя" in fake.alerts()[-1].text or "только главный" in fake.alerts()[-1].text
    await h.text(2, texts.BTN_CREATE)
    assert "Выберите дату" in fake.sent(2)[-1].text

    # новые заявки приходят и ему
    fake.reset()
    h.names[9] = "Новичок"
    await h.text(9, "/start")
    await h.click(9, "car:0")
    assert any("Новый игрок" in m.text for m in fake.sent(2)) and any("Новый игрок" in m.text for m in fake.sent(ADMIN))

    # обычный админ не может удалить другого админа; главный снимает права
    await h.click(ADMIN, f"pl:admin_on:{timur.id}")
    await h.click(2, f"pl:block:{timur.id}")
    assert "только главный админ" in fake.alerts()[-1].text
    await h.click(ADMIN, f"pl:admin_off:{arman.id}")
    assert any("Права администратора сняты" in m.text for m in fake.sent(2))
    fake.reset()
    await h.text(2, "/players")
    assert not any("Игроки" in m.text for m in fake.sent(2))
