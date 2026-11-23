"""🎒 Мячи/манишки с прошлой игры: держатель не идёт — бот просит передать заранее."""

from datetime import timedelta

from sqlalchemy import select

from bot import webhook
from bot.config import config
from bot.models import Assignment, Duty, Game, GameStatus
from bot.scheduler import tick
from tests.test_discipline import _member
from tests.test_miniapp import ADMIN, api, team, tg_user  # noqa: F401
from tests.test_webhook import fake  # noqa: F401


async def _setup(holder_id):
    """Прошлая тренировка: мячи забрал holder. Новая — завтра, сбор идёт."""
    day = (config.now() + timedelta(days=2)).date().isoformat()
    _, res = await api(ADMIN, "create_game", date=day, minutes=20 * 60, kind="training")
    gid = max(g["id"] for g in res["state"]["games"])
    async with webhook._sessionmaker() as s:
        balls = await s.scalar(select(Duty).where(Duty.code == "balls"))
        prev = Game(starts_at=config.now() - timedelta(days=3), status=GameStatus.FINISHED, kind="training",
                    after_duties_done=True)
        s.add(prev)
        await s.flush()
        s.add(Assignment(game_id=prev.id, user_id=holder_id, duty_id=balls.id))
        await s.commit()
        return gid, balls.id


def _gear(st, gid):
    return next(g for g in st["state"]["games"] if g["id"] == gid)["gear"]


async def test_holder_not_coming_hands_over(team):  # noqa: F811
    fake = team  # noqa: F811
    timur, nurik = tg_user(2, "Тимур"), tg_user(3, "Нурик")
    tid = await _member(timur)
    nid = await _member(nurik)
    gid, did = await _setup(tid)

    _, st = await api(timur, "state")
    g = _gear(st, gid)[0]
    assert g["mine"] and g["what"] == "мячи" and g["needs_handover"]  # ещё не ответил — пока не «Буду»
    await api(nurik, "rsvp", game_id=gid, status="yes")
    fake.reset()
    await api(timur, "rsvp", game_id=gid, status="no")
    dm = [m for m in fake.sent(2) if "Кому передашь" in (m.text or "")]
    assert dm and "Мячи у тебя" in dm[0].text
    assert [b.callback_data for row in dm[0].reply_markup.inline_keyboard for b in row] == [f"gh:{gid}:{did}:{nid}", f"gh:{gid}:{did}:0"]
    assert any("Мячи держит Тимур" in m.text for m in fake.sent(1))  # админу

    _, st = await api(timur, "state")
    g = _gear(st, gid)[0]
    assert g["needs_handover"] and [c["name"] for c in g["candidates"]] == ["Нурик"]
    # чужие вещи передать нельзя
    assert (await api(nurik, "gear_handover", game_id=gid, duty_id=did, to_user_id=nid))[0] == 400
    fake.reset()
    status, _ = await api(timur, "gear_handover", game_id=gid, duty_id=did, to_user_id=nid)
    assert status == 200
    assert any("передаст тебе" in m.text for m in fake.sent(3))
    _, st = await api(nurik, "state")
    g = _gear(st, gid)[0]
    assert g["mine"] and g["from"] == "Тимур" and g["coming"] and not g["needs_handover"]

    # теперь не идёт Нурик — просят уже его
    fake.reset()
    await api(nurik, "rsvp", game_id=gid, status="no")
    assert any("Кому передашь" in (m.text or "") for m in fake.sent(3))


async def test_silent_holder_warned_at_close_and_bot_button(team):  # noqa: F811
    fake = team  # noqa: F811
    timur = tg_user(2, "Тимур")
    tid = await _member(timur)
    gid, did = await _setup(tid)
    async with webhook._sessionmaker() as s:
        (await s.get(Game, gid)).rsvp_deadline_at = config.now() - timedelta(minutes=1)
        await s.commit()
    fake.reset()
    async with webhook._sessionmaker() as s:
        await tick(webhook.make_bot(), s)
        await s.commit()
    assert any("Кому передашь" in (m.text or "") for m in fake.sent(2))
    assert any("Мячи держит Тимур" in (m.text or "") for m in fake.sent(1))

    # «Договорюсь сам» — больше не тревожим
    status, _ = await api(timur, "gear_handover", game_id=gid, duty_id=did, to_user_id=0)
    assert status == 200
    _, st = await api(ADMIN, "state")
    g = _gear(st, gid)[0]
    assert g["self_arranged"] and not g["needs_handover"]


async def test_only_nearest_game_counts(team):  # noqa: F811
    timur = tg_user(2, "Тимур")
    tid = await _member(timur)
    gid, _ = await _setup(tid)
    day = (config.now() + timedelta(days=5)).date().isoformat()
    _, res = await api(ADMIN, "create_game", date=day, minutes=20 * 60, kind="training")
    later = max(g["id"] for g in res["state"]["games"])
    _, st = await api(timur, "state")
    assert _gear(st, gid) and _gear(st, later) == []  # ко второй игре мячи поедут уже с первой


async def _tick():
    async with webhook._sessionmaker() as s:
        await tick(webhook.make_bot(), s)
        await s.commit()


async def test_game_day_reminder(team):  # noqa: F811
    fake = team  # noqa: F811
    timur, nurik = tg_user(2, "Тимур"), tg_user(3, "Нурик")
    tid = await _member(timur)
    await _member(nurik)
    gid, did = await _setup(tid)
    await api(timur, "rsvp", game_id=gid, status="yes")

    fake.reset()
    await _tick()  # игра через 2 дня — рано
    assert not any("Не забудь" in (m.text or "") for m in fake.sent(2))

    async with webhook._sessionmaker() as s:  # день игры, до начала 2 часа
        (await s.get(Game, gid)).starts_at = config.now() + timedelta(hours=2)
        await s.commit()
    await _tick()
    assert [m.text for m in fake.sent(2) if "Не забудь" in (m.text or "")] and "мячи" in fake.sent(2)[-1].text
    fake.reset()
    await _tick()  # второй раз не напоминает
    assert not any("Не забудь" in (m.text or "") for m in fake.sent(2))


async def test_game_day_reminder_not_coming_asks_again(team):  # noqa: F811
    fake = team  # noqa: F811
    timur = tg_user(2, "Тимур")
    tid = await _member(timur)
    gid, _ = await _setup(tid)
    await api(timur, "rsvp", game_id=gid, status="no")
    async with webhook._sessionmaker() as s:
        (await s.get(Game, gid)).starts_at = config.now() + timedelta(hours=2)
        await s.commit()
    fake.reset()
    await _tick()
    assert any("Игра сегодня" in (m.text or "") and "Кому передашь" in m.text for m in fake.sent(2))
