"""Кнопки в боте — как в приложении: «⏰ Опаздываю», «🧮 Почему так», админу «⏰ Отметить опоздание»."""

from datetime import timedelta

from sqlalchemy import select

from bot import texts
from bot.config import config
from bot.models import Game, GameParticipant
from bot.services import games as svc
from tests.test_bot_flow import ADMIN, Harness, _move_game, buttons, h  # noqa: F401 — фикстура h
from tests.test_miniapp import team  # noqa: F401
from tests.test_webhook import fake  # noqa: F401


async def _game_with(h: Harness, *yes):
    await h.text(ADMIN, texts.BTN_CREATE)
    tomorrow = config.now() + timedelta(days=1)
    await h.click(ADMIN, f"newdate:{tomorrow:%Y-%m-%d}")
    await h.text(ADMIN, "20:00")
    await h.click(ADMIN, "newkind:training")
    await h.text(ADMIN, "Стадион")
    await h.click(ADMIN, "newgame:publish")
    async with h.sm() as s:
        gid = (await s.scalars(select(Game.id))).all()[-1]
    for uid in yes:
        await h.click(uid, f"rsvp:{gid}:yes")
    return gid


def _all_buttons(msgs):
    return [b for m in msgs if m.reply_markup and hasattr(m.reply_markup, "inline_keyboard") for b in buttons(m.reply_markup)]


async def test_late_buttons_in_bot(h: Harness):
    fake = h.fake
    await h.register(ADMIN, "Даниял", car=True)
    await h.register(2, "Нурик", car=False)
    gid = await _game_with(h, ADMIN, 2)

    # в меню игрока есть «⏰ Опаздываю»; до дня игры — подсказка
    menu = [m for m in fake.sent(2) if m.reply_markup is not None and hasattr(m.reply_markup, "keyboard")][-1]
    assert texts.BTN_LATE in [b.text for row in menu.reply_markup.keyboard for b in row]
    fake.reset()
    await h.text(2, texts.BTN_LATE)
    assert "в день игры" in fake.sent(2)[-1].text

    # день игры: кнопка есть и в карточке игры
    await _move_game(h, gid, timedelta(hours=3))
    fake.reset()
    await h.text(2, texts.BTN_CURRENT)
    assert f"lt:{gid}" in [b.callback_data for b in _all_buttons(fake.sent(2))]

    # Опаздываю → 15 мин → причина текстом → админам одно короткое сообщение
    fake.reset()
    await h.text(2, texts.BTN_LATE)
    assert "Примерно на сколько" in fake.sent(2)[-1].text
    await h.click(2, f"lt:{gid}:15")
    await h.text(2, "Съемки на работе")
    assert any("Админы знают" in m.text for m in fake.sent(2))
    admin_msgs = [m.text for m in fake.sent(ADMIN) if "опоздает" in m.text]
    assert admin_msgs == [f"⏰ Нурик опоздает на ~15 мин ({texts.game_header(await _game(h, gid))}): Съемки на работе"]
    # «Успеваю — отменить»
    await h.click(2, f"lt:{gid}:0")
    async with h.sm() as s:
        nur = await svc.get_user_by_tg(s, 2)
        assert (await s.get(GameParticipant, (gid, nur.id))).late_notice_minutes is None

    # админ: игра началась → «⏰ Отметить опоздание» → игрок → 20 мин → −1
    await _move_game(h, gid, timedelta(minutes=-20))
    fake.reset()
    await h.text(ADMIN, texts.BTN_CURRENT)
    assert f"lta:{gid}" in [b.callback_data for b in _all_buttons(fake.sent(ADMIN))]
    await h.click(ADMIN, f"lta:{gid}")
    await h.click(ADMIN, f"lta:{gid}:{nur.id}")
    await h.click(ADMIN, f"lta:{gid}:{nur.id}:20:0")
    async with h.sm() as s:
        assert (await svc.open_penalty_points(s, [nur.id])).get(nur.id) == 1
    assert any("опоздал на 20 мин" in m.text for m in fake.sent(2))
    # форс-мажор — минус снят
    await h.click(ADMIN, f"lta:{gid}:{nur.id}:20:1")
    async with h.sm() as s:
        assert not (await svc.open_penalty_points(s, [nur.id])).get(nur.id)
        me = await svc.get_user_by_tg(s, ADMIN)
    # себе админ форс-мажор не ставит
    await h.click(ADMIN, f"lta:{gid}:{me.id}:20:0")
    await h.click(ADMIN, f"lta:{gid}:{me.id}:20:1")
    async with h.sm() as s:
        assert (await svc.open_penalty_points(s, [me.id])).get(me.id) == 1


async def _game(h, gid):
    async with h.sm() as s:
        return await s.get(Game, gid)


async def test_why_button_in_bot(h: Harness):
    fake = h.fake
    await h.register(ADMIN, "Даниял", car=True)
    await h.register(2, "Рустам", car=False)
    gid = await _game_with(h, ADMIN, 2)
    await h.click(ADMIN, f"adm:dist:{gid}")
    fake.reset()
    await h.text(2, texts.BTN_CURRENT)
    assert f"why:{gid}" in [b.callback_data for b in _all_buttons(fake.sent(2))]
    await h.click(2, f"why:{gid}")
    text = fake.sent(2)[-1].text
    assert "Почему так распределилось" in text and "Как бот выбирает" in text
    assert "Почему тебе" in text or "А ты?" in text


async def test_opponent_in_bot_and_app(h: Harness):
    fake = h.fake
    await h.register(ADMIN, "Даниял", car=True)
    await h.text(ADMIN, texts.BTN_CREATE)
    tomorrow = config.now() + timedelta(days=1)
    await h.click(ADMIN, f"newdate:{tomorrow:%Y-%m-%d}")
    await h.text(ADMIN, "20:00")
    await h.click(ADMIN, "newkind:game")
    assert "Против кого" in fake.sent(ADMIN)[-1].text
    await h.text(ADMIN, "  «Барыс» ")
    await h.text(ADMIN, "Стадион")
    assert "против «Барыс»" in fake.sent(ADMIN)[-1].text  # в предпросмотре
    await h.click(ADMIN, "newgame:publish")
    async with h.sm() as s:
        game = (await s.scalars(select(Game))).all()[-1]
        assert game.opponent == "Барыс"
        assert texts.game_header(game).startswith("⚽ Игра против «Барыс» — ")
        assert await svc.opponents(s) == ["Барыс"]


async def test_opponent_change_and_training_has_none(team):  # noqa: F811
    from tests.test_miniapp import ADMIN as APP_ADMIN, api

    day = (config.now() + timedelta(days=2)).date().isoformat()
    _, res = await api(APP_ADMIN, "create_game", date=day, minutes=19 * 60, kind="game", opponent="Кайрат")
    g = res["state"]["games"][0]
    assert g["opponent"] == "Кайрат" and g["kind_title"] == "⚽ Игра против «Кайрат»"
    assert res["state"]["opponents"] == ["Кайрат"]
    status, res = await api(APP_ADMIN, "update_game", game_id=g["id"], date=day, minutes=19 * 60, kind="game", opponent="Астана")
    assert status == 200 and next(x for x in res["state"]["games"] if x["id"] == g["id"])["opponent"] == "Астана"
    # тренировка — без соперника, даже если прислали
    _, res = await api(APP_ADMIN, "update_game", game_id=g["id"], date=day, minutes=19 * 60, kind="training", opponent="Астана")
    t = next(x for x in res["state"]["games"] if x["id"] == g["id"])
    assert t["opponent"] is None and t["kind_title"] == "🏃 Тренировка"
