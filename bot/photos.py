"""Фото игроков для аватаров: обрезаем в квадрат, пережимаем в JPEG, отдаём по подписанной ссылке.

Ссылку получают только те, кто в команде (она приходит в состоянии приложения), — посторонний её не угадает.
"""

import base64
import hashlib
import hmac
import io
import json

from PIL import Image, ImageOps
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import config
from bot.models import User, UserPhoto

SIZE = 400
# Внешний вид аватара: телосложение, кожа, бутсы, празднование. Значения проверяем по этому списку.
STYLE_OPTIONS = {
    "build": ("normal", "slim", "small", "athletic", "stocky", "tall"),
    "skin": ("1", "2", "3", "4", "5"),
    "boots": ("black", "white", "gold", "red", "blue", "neon", "pink"),
    "celebration": ("goal", "siu", "sky", "fist", "zen", "arms"),
}


def clean_style(raw: dict | None) -> dict:
    raw = raw if isinstance(raw, dict) else {}
    return {k: str(raw[k]) for k, allowed in STYLE_OPTIONS.items() if str(raw.get(k)) in allowed}


def style_of(user: User) -> dict:
    try:
        return clean_style(json.loads(user.avatar_style or "{}"))
    except ValueError:
        return {}


MAX_UPLOAD = 6 * 1024 * 1024  # исходник до 6 МБ (обычно приложение присылает уже уменьшенное)


class PhotoError(ValueError):
    pass


def process(raw: bytes) -> bytes:
    """Любое фото → квадрат SIZE×SIZE, JPEG. Заодно убирает всё лишнее из файла (EXIF, геометку)."""
    if not raw or len(raw) > MAX_UPLOAD:
        raise PhotoError("Фото слишком большое — до 6 МБ.")
    try:
        img = Image.open(io.BytesIO(raw))
        img.load()
    except Exception as e:  # noqa: BLE001
        raise PhotoError("Не получилось открыть фото. Попробуйте другое (JPG или PNG).") from e
    img = ImageOps.exif_transpose(img).convert("RGB")
    w, h = img.size
    side = min(w, h)
    # Лицо на портрете обычно выше центра — режем квадрат чуть выше середины.
    top = int((h - side) * 0.3)
    left = (w - side) // 2
    img = img.crop((left, top, left + side, top + side)).resize((SIZE, SIZE), Image.LANCZOS)
    out = io.BytesIO()
    img.save(out, "JPEG", quality=84, optimize=True)
    return out.getvalue()


def decode_upload(data: str) -> bytes:
    """Из приложения приходит data:URL или голый base64."""
    if "," in data[:100]:
        data = data.split(",", 1)[1]
    try:
        return base64.b64decode(data, validate=True)
    except Exception as e:  # noqa: BLE001
        raise PhotoError("Фото повредилось при загрузке — попробуйте ещё раз.") from e


def _sig(user_id: int, version: int) -> str:
    key = hashlib.sha256(f"photo:{config.bot_token}".encode()).digest()
    return hmac.new(key, f"{user_id}.{version}".encode(), hashlib.sha256).hexdigest()[:20]


def url(user: User) -> str | None:
    if not user.photo_version:
        return None
    return f"/api/app?photo={user.id}.{user.photo_version}.{_sig(user.id, user.photo_version)}"


def parse(token: str) -> tuple[int, int] | None:
    try:
        raw_id, raw_ver, sig = (token or "").split(".")
        user_id, version = int(raw_id), int(raw_ver)
    except ValueError:
        return None
    if not config.bot_token or not hmac.compare_digest(_sig(user_id, version), sig):
        return None
    return user_id, version


async def save(session: AsyncSession, user: User, raw: bytes) -> None:
    data = process(raw)
    row = await session.get(UserPhoto, user.id)
    if row is None:
        session.add(UserPhoto(user_id=user.id, data=data))
    else:
        row.data = data
    user.photo_version = (user.photo_version or 0) + 1
    await session.flush()


async def delete(session: AsyncSession, user: User) -> None:
    row = await session.get(UserPhoto, user.id)
    if row is not None:
        await session.delete(row)
    user.photo_version = 0
    await session.flush()


async def load(session: AsyncSession, token: str) -> bytes | None:
    parsed = parse(token)
    if parsed is None:
        return None
    user_id, version = parsed
    user = await session.get(User, user_id)
    if user is None or user.photo_version != version:
        return None
    row = await session.get(UserPhoto, user_id)
    return row.data if row else None
