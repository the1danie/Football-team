"""Библиотека комбинаций на стандартах: по 2 на угловых, аутах и штрафных — схема, описание, роли игроков.

Ведут тренер, капитан и админы; видят все. После матча отмечают, какая комбинация принесла гол.
"""

import hashlib
import hmac
import io
import time

from PIL import Image, ImageOps
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import config
from bot.models import ComboGoals, Game, GameStatus, GoalSource, SetPiece, SetPieceImage, User
from bot.photos import MAX_UPLOAD, PhotoError

KINDS = {GoalSource.CORNER: "🚩 Угловые", GoalSource.THROW_IN: "🤾 Ауты", GoalSource.FREE_KICK: "🎯 Штрафные"}
STATUSES = {"learning": "📚 Разучиваем", "ready": "✅ Отработана"}
TARGET_PER_KIND = 2
MAX_COMBOS = 30
MAX_ROLES = 10
IMAGE_SIDE = 1200


class ComboError(Exception):
    pass


def _clean(text: object, limit: int) -> str:
    return " ".join(str(text or "").split())[:limit]


def _clean_multiline(text: object, limit: int) -> str:
    lines = [" ".join(line.split()) for line in str(text or "").splitlines()]
    return "\n".join(lines).strip()[:limit]


async def save(session: AsyncSession, body: dict) -> SetPiece:
    combo = await session.get(SetPiece, int(body["id"])) if body.get("id") else None
    if combo is None and await session.scalar(select(func.count()).select_from(SetPiece)) >= MAX_COMBOS:
        raise ComboError(f"В библиотеке уже {MAX_COMBOS} комбинаций — удалите лишние.")
    kind = str(body.get("kind") or "")
    if kind not in KINDS:
        raise ComboError("Выберите вид: угловой, аут или штрафной.")
    name = _clean(body.get("name"), 60)
    if not name:
        raise ComboError("Назовите комбинацию — так её будут звать на поле.")
    status = body.get("status") if body.get("status") in STATUSES else "learning"
    roles = []
    for raw in (body.get("roles") or [])[:MAX_ROLES]:
        role = _clean(raw.get("role"), 60)
        uid = int(raw.get("user_id") or 0) or None
        if uid is not None and await session.get(User, uid) is None:
            uid = None
        if role:
            roles.append({"role": role, "user_id": uid})
    if combo is None:
        combo = SetPiece(kind=kind, name=name)
        session.add(combo)
    combo.kind, combo.name, combo.status, combo.roles = kind, name, status, roles
    combo.description = _clean_multiline(body.get("description"), 1500)
    await session.flush()
    return combo


async def delete(session: AsyncSession, combo_id: int) -> None:
    combo = await session.get(SetPiece, combo_id)
    if combo is None:
        return
    for model in (SetPieceImage, ComboGoals):
        for row in (await session.scalars(select(model).where(model.set_piece_id == combo_id))).all():
            await session.delete(row)
    await session.delete(combo)
    await session.flush()


def process_image(raw: bytes) -> bytes:
    """Схема: без обрезки, длинная сторона до IMAGE_SIDE, JPEG без EXIF."""
    if not raw or len(raw) > MAX_UPLOAD:
        raise PhotoError("Фото слишком большое — до 6 МБ.")
    try:
        img = Image.open(io.BytesIO(raw))
        img.load()
    except Exception as e:  # noqa: BLE001
        raise PhotoError("Не получилось открыть фото. Попробуйте другое (JPG или PNG).") from e
    img = ImageOps.exif_transpose(img).convert("RGB")
    img.thumbnail((IMAGE_SIDE, IMAGE_SIDE), Image.LANCZOS)
    out = io.BytesIO()
    img.save(out, "JPEG", quality=85, optimize=True)
    return out.getvalue()


async def set_image(session: AsyncSession, combo: SetPiece, raw: bytes | None) -> None:
    row = await session.get(SetPieceImage, combo.id)
    if raw is None:
        if row is not None:
            await session.delete(row)
        combo.image_version = -abs(combo.image_version or 0)
    else:
        data = process_image(raw)
        if row is None:
            session.add(SetPieceImage(set_piece_id=combo.id, data=data))
        else:
            row.data = data
        combo.image_version = max(int(time.time()), abs(combo.image_version or 0) + 1)
    await session.flush()


def _sig(combo_id: int, version: int) -> str:
    key = hashlib.sha256(f"scheme:{config.bot_token}".encode()).digest()
    return hmac.new(key, f"{combo_id}.{version}".encode(), hashlib.sha256).hexdigest()[:20]


def image_url(combo: SetPiece) -> str | None:
    if (combo.image_version or 0) <= 0:
        return None
    return f"/api/app?scheme={combo.id}.{combo.image_version}.{_sig(combo.id, combo.image_version)}"


async def load_image(session: AsyncSession, token: str) -> bytes | None:
    try:
        raw_id, raw_ver, sig = (token or "").split(".")
        combo_id, version = int(raw_id), int(raw_ver)
    except ValueError:
        return None
    if not config.bot_token or not hmac.compare_digest(_sig(combo_id, version), sig):
        return None
    combo = await session.get(SetPiece, combo_id)
    if combo is None or version <= 0 or combo.image_version != version:
        return None
    row = await session.get(SetPieceImage, combo_id)
    return row.data if row else None


async def set_goals(session: AsyncSession, game: Game, combo_id: int, count: object) -> None:
    if await session.get(SetPiece, combo_id) is None:
        raise ComboError("Комбинация не найдена.")
    count = max(0, min(30, int(count or 0)))
    row = await session.scalar(select(ComboGoals).where(ComboGoals.game_id == game.id, ComboGoals.set_piece_id == combo_id))
    if count == 0:
        if row is not None:
            await session.delete(row)
    elif row is None:
        session.add(ComboGoals(game_id=game.id, set_piece_id=combo_id, count=count))
    else:
        row.count = count
    await session.flush()


async def view(session: AsyncSession, game_ids: list[int] | None = None) -> dict:
    """Библиотека: по видам, с ролями и голами (в матчах / на тренировках); per_game — голы комбинаций по играм."""
    combos = (await session.scalars(select(SetPiece).order_by(SetPiece.sort_order, SetPiece.id))).all()
    rows = (await session.execute(
        select(ComboGoals.game_id, ComboGoals.set_piece_id, ComboGoals.count, Game.kind)
        .join(Game, Game.id == ComboGoals.game_id).where(Game.status.not_in(GameStatus.NOT_PLAYED))
    )).all()
    ids = {uid for c in combos for r in (c.roles or []) if (uid := r.get("user_id"))}
    names = {u.id: u.name for u in (await session.scalars(select(User).where(User.id.in_(ids)))).all()} if ids else {}
    items = []
    for c in combos:
        mine = [r for r in rows if r.set_piece_id == c.id]
        items.append({
            "id": c.id, "kind": c.kind, "name": c.name, "description": c.description, "status": c.status,
            "image": image_url(c),
            "roles": [{"role": r["role"], "user_id": r.get("user_id"), "name": names.get(r.get("user_id"))}
                      for r in (c.roles or [])],
            "goals_matches": sum(r.count for r in mine if r.kind == "match"),
            "goals_trainings": sum(r.count for r in mine if r.kind != "match"),
        })
    per_game: dict[int, dict[int, int]] = {}
    for r in rows:
        if game_ids is None or r.game_id in game_ids:
            per_game.setdefault(r.game_id, {})[r.set_piece_id] = r.count
    return {"kinds": KINDS, "statuses": STATUSES, "target": TARGET_PER_KIND, "items": items, "per_game": per_game}
