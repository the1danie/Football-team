"""«⚖️ Итог» в карточке игрока: та же арифметика, что в рейтинге, по пунктам; будущая обязанность не считается."""

from datetime import timedelta

from bot import webhook
from bot.config import config
from bot.models import Assignment, Duty, Game, GameStatus, Penalty, PenaltyStatus, User
from sqlalchemy import select
from tests.test_miniapp import ADMIN, api, team, tg_user  # noqa: F401
from tests.test_webhook import fake  # noqa: F401


async def test_balance_breakdown_matches_board(team):  # noqa: F811
    timur = tg_user(2, "Тимур")
    await api(timur, "register", car=True, name="Тимур")
    _, pl = await api(ADMIN, "players")
    tid = next(p["id"] for p in pl["players"] if p["name"] == "Тимур")
    await api(ADMIN, "player", user_id=tid, op="approve")

    now = config.now()
    async with webhook._sessionmaker() as s:
        water = Duty(code="water_t", emoji="💧", name="Вода", action="принести воду")
        s.add(water)
        missed = Game(starts_at=now - timedelta(days=3), status=GameStatus.FINISHED, kind="training")
        played = Game(starts_at=now - timedelta(days=1), status=GameStatus.FINISHED, kind="training")
        future = Game(starts_at=now + timedelta(days=1), status=GameStatus.DISTRIBUTED, kind="training")
        s.add_all([missed, played, future])
        await s.flush()
        s.add_all([Assignment(game_id=played.id, user_id=tid, duty_id=water.id),
                   Assignment(game_id=future.id, user_id=tid, duty_id=water.id)])
        # пропустил игру (−1), минус отработал водой на следующей
        s.add(Penalty(user_id=tid, game_id=missed.id, points=1, status=PenaltyStatus.REDEEMED, redeemed_game_id=played.id))
        await s.commit()

    _, r = await api(ADMIN, "player_stats", user_id=tid, period="all")
    assert sum(d["count"] for d in r["duties"] if d["name"] == "Вода") == 1  # будущая вода — ещё не выполнена
    b = r["balance"]
    assert (b["help_total"], b["minus_total"], b["total"]) == (1, 1, 0)
    assert b["minuses"][0]["state"].startswith("отработан")
    _, st = await api(ADMIN, "stats", period="all")
    assert next(x for x in st["board"] if x["id"] == tid)["balance"] == b["total"]
