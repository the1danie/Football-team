"""Посещаемость в PDF: таблица с процентами и историей последних игр цветными клетками."""

import hashlib
import hmac
import time
from datetime import timedelta
from pathlib import Path

from fpdf import FPDF
from sqlalchemy.ext.asyncio import AsyncSession

from bot import texts
from bot.config import config
from bot.services import games as svc

LINK_TTL = 24 * 3600  # ссылка на PDF живёт сутки
FONTS = Path(__file__).parent / "fonts"

# Цвета клеток истории (как значки в приложении).
KIND_COLORS = {
    "came": (46, 160, 67), "no": (210, 60, 60), "maybe": (230, 180, 40),
    "silent": (150, 150, 150), "no_show": (120, 20, 20),
}
KIND_LABELS = {"came": "был", "no": "не буду", "maybe": "не знаю", "silent": "молчал", "no_show": "сказал «Буду» и не пришёл"}


def _sig(payload: str) -> str:
    key = hashlib.sha256(f"report:{config.bot_token}".encode()).digest()
    return hmac.new(key, payload.encode(), hashlib.sha256).hexdigest()[:24]


def make_token(telegram_id: int, period: str, now: float | None = None) -> str:
    payload = f"{telegram_id}.{period}.{int((now or time.time()) + LINK_TTL)}"
    return f"{payload}.{_sig(payload)}"


def parse_token(token: str, now: float | None = None) -> tuple[int, str] | None:
    try:
        raw_id, period, exp, sig = (token or "").split(".")
        telegram_id, expires = int(raw_id), int(exp)
    except ValueError:
        return None
    payload = f"{raw_id}.{period}.{exp}"
    if not config.bot_token or not hmac.compare_digest(_sig(payload), sig):
        return None
    if expires < (now or time.time()) or period not in ("month", "all"):
        return None
    return telegram_id, period


async def from_token(session: AsyncSession, token: str, team: str) -> tuple[bytes, str] | None:
    """PDF по ссылке — если ссылка цела и человек всё ещё админ или в штабе."""
    parsed = parse_token(token)
    if parsed is None:
        return None
    telegram_id, period = parsed
    user = await svc.get_user_by_tg(session, telegram_id)
    if not config.is_admin(telegram_id) and not (user is not None and user.is_staff):
        return None
    now = config.now()
    since = now - timedelta(days=30) if period == "month" else None
    rows = [r for r in await svc.attendance_report(session, now, since) if r["games"]]
    title = "за последний месяц" if since else "за всё время"
    filename = f"Посещаемость {team} {now:%d.%m.%Y}.pdf"
    return build(rows, team, title, texts.fmt_date(now)), filename


def build(rows: list[dict], team: str, period_title: str, date_label: str) -> bytes:
    pdf = FPDF(orientation="P", unit="mm", format="A4")
    pdf.set_auto_page_break(auto=True, margin=12)
    pdf.add_font("DejaVu", "", str(FONTS / "DejaVuSans.ttf"))
    pdf.add_font("DejaVu", "B", str(FONTS / "DejaVuSans-Bold.ttf"))
    pdf.set_title(f"Посещаемость — {team}")
    pdf.add_page()

    pdf.set_font("DejaVu", "B", 16)
    pdf.cell(0, 9, f"Посещаемость — {team}", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("DejaVu", "", 10)
    pdf.set_text_color(90, 90, 90)
    pdf.cell(0, 6, f"{period_title.capitalize()} · на {date_label} · игроков: {len(rows)}", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(2)

    # легенда
    pdf.set_font("DejaVu", "", 8)
    for kind, label in KIND_LABELS.items():
        pdf.set_fill_color(*KIND_COLORS[kind])
        pdf.rect(pdf.get_x(), pdf.get_y() + 1.2, 3, 3, style="F")
        pdf.set_x(pdf.get_x() + 4)
        pdf.cell(pdf.get_string_width(label) + 5, 5, label)
    pdf.ln(7)
    pdf.set_text_color(0, 0, 0)

    problem = [r["name"] for r in rows
               if (r["games"] >= 2 and (r["rate"] < 50 or r["silent"] + r["no_show"] >= 2)) or r["failed"] or r["gave"] >= 2]
    if problem:
        pdf.set_fill_color(255, 243, 205)
        pdf.set_font("DejaVu", "B", 10)
        pdf.multi_cell(0, 6, f"Требуют внимания ({len(problem)}): " + ", ".join(problem), fill=True,
                       new_x="LMARGIN", new_y="NEXT")
        pdf.ln(3)

    cols = [("Игрок", 44), ("%", 13), ("Был", 15), ("Не буду", 16), ("Не знаю", 16), ("Молчал", 16),
            ("Неявка", 15), ("Последние игры", 55)]

    def header() -> None:
        pdf.set_font("DejaVu", "B", 8)
        pdf.set_fill_color(235, 235, 235)
        for name, w in cols:
            pdf.cell(w, 7, name, border=1, align="C", fill=True)
        pdf.ln()

    header()
    pdf.set_font("DejaVu", "", 9)
    for i, r in enumerate(rows):
        if pdf.get_y() > 280:
            pdf.add_page()
            header()
            pdf.set_font("DejaVu", "", 9)
        fill = i % 2 == 1
        pdf.set_fill_color(248, 248, 248)
        name = r["name"] + (" ✍" if r["manual"] else "")
        while pdf.get_string_width(name) > cols[0][1] - 2 and len(name) > 3:
            name = name[:-2] + "…"
        pdf.cell(cols[0][1], 7, name, border=1, fill=fill)
        rate = r["rate"]
        color = (190, 40, 40) if rate < 50 else (200, 130, 0) if rate < 75 else (30, 130, 50)
        pdf.set_text_color(*color)
        pdf.set_font("DejaVu", "B", 9)
        pdf.cell(cols[1][1], 7, f"{rate}%", border=1, align="C", fill=fill)
        pdf.set_text_color(0, 0, 0)
        pdf.set_font("DejaVu", "", 9)
        pdf.cell(cols[2][1], 7, f"{r['came']} из {r['games']}", border=1, align="C", fill=fill)
        for (_, w), key in zip(cols[3:7], ("no", "maybe", "silent", "no_show"), strict=True):
            pdf.cell(w, 7, str(r[key] or "—"), border=1, align="C", fill=fill)
        x, y = pdf.get_x(), pdf.get_y()
        pdf.cell(cols[7][1], 7, "", border=1, fill=fill)
        for k, kind in enumerate(r.get("kinds", [])):
            pdf.set_fill_color(*KIND_COLORS[kind])
            pdf.rect(x + 2 + k * 5.2, y + 1.6, 4, 4, style="F")
        pdf.ln()

    if not rows:
        pdf.set_font("DejaVu", "", 10)
        pdf.cell(0, 8, "Прошедших игр за этот период нет.", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(3)
    pdf.set_font("DejaVu", "", 8)
    pdf.set_text_color(110, 110, 110)
    pdf.multi_cell(0, 4.5, "Сверху — кто пропускает чаще. «Последние игры» — до 10 игр, слева направо (старые → новые). "
                            "Процент — сколько игр был из тех, что прошли, пока игрок в команде.")
    return bytes(pdf.output())
