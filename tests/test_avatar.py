"""Аватары: фото (обрезка, ссылка только для команды), номер на футболке, кто что может менять."""

import base64
import io

from PIL import Image

from tests.test_calendar import fetch
from tests.test_miniapp import ADMIN, api, team, tg_user  # noqa: F401 — фикстура team
from tests.test_webhook import fake  # noqa: F401


def _photo(w=900, h=1200) -> str:
    buf = io.BytesIO()
    Image.new("RGB", (w, h), (200, 30, 30)).save(buf, "PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


async def _member(user):
    await api(user, "register", car=False)
    _, pl = await api(ADMIN, "players")
    uid = next(p["id"] for p in pl["players"] if p["name"] == user["first_name"])
    await api(ADMIN, "player", user_id=uid, op="approve")
    return uid


async def test_photo_and_number(team):  # noqa: F811
    pasha, rus = tg_user(2, "Паша"), tg_user(3, "Рус")
    pasha_id = await _member(pasha)
    rus_id = await _member(rus)

    status, res = await api(pasha, "avatar", op="photo", image=_photo())
    assert status == 200 and res["note"] == "📷 Фото сохранено"
    url = res["state"]["user"]["photo"]
    assert url.startswith("/api/app?photo=")
    _, st = await api(rus, "state")
    assert st["state"]["faces"][str(pasha_id)]["photo"] == url  # команда видит
    code, headers, body = await fetch(url.split("?", 1)[1])
    assert code == 200 and headers[b"content-type"] == b"image/jpeg"
    assert Image.open(io.BytesIO(body)).size == (320, 320)

    token = url.split("photo=")[1]
    assert (await fetch("photo=" + token[:-2] + "00"))[0] == 404  # подделка
    await api(pasha, "avatar", op="photo", image=_photo(500, 500))  # заменил — старая ссылка не работает
    assert (await fetch("photo=" + token))[0] == 404

    assert (await api(rus, "avatar", op="photo", image=_photo(), user_id=pasha_id))[0] == 403  # чужое — нельзя
    status, _ = await api(ADMIN, "avatar", op="number", number="10", user_id=rus_id)  # админ — может
    assert status == 200
    _, st = await api(rus, "state")
    assert st["state"]["user"]["number"] == 10 and st["state"]["faces"][str(rus_id)]["number"] == 10
    assert (await api(rus, "avatar", op="number", number="150"))[0] == 400
    assert (await api(rus, "avatar", op="number", number="abc"))[0] == 400
    assert (await api(rus, "avatar", op="photo", image="data:image/png;base64,bm90IGFuIGltYWdl"))[0] == 400

    _, res = await api(pasha, "avatar", op="photo_delete")
    assert res["state"]["user"]["photo"] is None
    _, log = await api(ADMIN, "audit")
    assert any("Аватар Рус: номер 10" in e["text"] for e in log["entries"])
