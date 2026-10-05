"""Ссылка на место в 2ГИС: подстановка, запоминание по месту, тексты."""

from datetime import timedelta

from bot import texts
from bot.config import config
from bot.models import Game
from tests.test_miniapp import ADMIN, api, team  # noqa: F401 — фикстура team
from tests.test_webhook import fake  # noqa: F401

GIS = "https://2gis.kz/petropavlovsk/firm/70000001234567"


def test_normalize_and_fallback():
    assert texts.normalize_map_url("  Смотри: https://go.2gis.com/abc12, тут") == "https://go.2gis.com/abc12"
    assert texts.normalize_map_url("просто текст") is None
    assert texts.normalize_map_url(None) is None
    assert texts.map_url(None) is None
    search = texts.map_url("Жас Оркен")
    assert search.startswith(f"https://2gis.kz/{config.twogis_city}/search/") and "%D0%96" in search
    assert texts.map_url("Жас Оркен", GIS) == GIS
    assert texts.map_label(search) == "2ГИС" and texts.map_label("https://maps.app.goo.gl/x") == "Карта"


def test_texts_contain_link():
    game = Game(id=1, kind="game", starts_at=config.now(), location="Динамо", location_url=GIS, status="open")
    assert f'<a href="{GIS}">Динамо</a>' in texts.location_line(game)


async def test_link_remembered_per_place(team):  # noqa: F811
    day1 = (config.now() + timedelta(days=1)).date().isoformat()
    day2 = (config.now() + timedelta(days=2)).date().isoformat()
    status, res = await api(ADMIN, "create_game", date=day1, minutes=20 * 60, kind="game",
                            location="Жас Оркен", location_url=f"вот {GIS}")
    assert status == 200, res
    g1 = res["state"]["games"][0]
    assert g1["map_url"] == GIS and g1["map_label"] == "2ГИС" and g1["location_url"] == GIS
    assert f"2ГИС: {GIS}" in res["whatsapp"]["text"]
    assert {"name": "Жас Оркен", "url": GIS} in res["state"]["places"]

    # то же место без ссылки — ссылка подставилась сама
    _, res = await api(ADMIN, "create_game", date=day2, minutes=20 * 60, kind="game", location="Жас Оркен")
    g2 = next(g for g in res["state"]["games"] if g["id"] != g1["id"])
    assert g2["location_url"] == GIS

    # другое место без ссылки — поиск по названию
    _, res = await api(ADMIN, "update_game", game_id=g2["id"], date=day2, minutes=20 * 60, kind="game",
                       location="Динамо", min_players=0)
    g2 = next(g for g in res["state"]["games"] if g["id"] == g2["id"])
    assert g2["location_url"] is None and "/search/" in g2["map_url"]
