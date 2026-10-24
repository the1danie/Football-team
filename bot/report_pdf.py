"""Отчёты в PDF: посещаемость (таблица с процентами и историей игр цветными клетками) и «Итог» (помощь − минусы)."""

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


KINDS = ("att", "bal")  # посещаемость, итог


def make_token(telegram_id: int, period: str, now: float | None = None, kind: str = "att") -> str:
    payload = f"{kind}.{telegram_id}.{period}.{int((now or time.time()) + LINK_TTL)}"
    return f"{payload}.{_sig(payload)}"


def parse_token(token: str, now: float | None = None) -> tuple[int, str, str] | None:
    try:
        kind, raw_id, period, exp, sig = (token or "").split(".")
        telegram_id, expires = int(raw_id), int(exp)
    except ValueError:
        return None
    payload = f"{kind}.{raw_id}.{period}.{exp}"
    if not config.bot_token or not hmac.compare_digest(_sig(payload), sig):
        return None
    if expires < (now or time.time()) or period not in ("month", "all") or kind not in KINDS:
        return None
    return telegram_id, period, kind


async def from_token(session: AsyncSession, token: str, team: str) -> tuple[bytes, str] | None:
    """PDF по ссылке. Посещаемость — админ или штаб; итог — любой из команды (он и так виден в «Рейтинге»)."""
    parsed = parse_token(token)
    if parsed is None:
        return None
    telegram_id, period, kind = parsed
    user = await svc.get_user_by_tg(session, telegram_id)
    allowed = config.is_admin(telegram_id) or (user is not None and user.is_staff)
    if kind == "bal":
        allowed = allowed or (user is not None and user.is_approved)
    if not allowed:
        return None
    now = config.now()
    since = now - timedelta(days=30) if period == "month" else None
    title = "за последний месяц" if since else "за всё время"
    if kind == "bal":
        return await balance_pdf(session, team, now, since, title)
    return await attendance_pdf(session, team, now, since, title)


async def attendance_pdf(session: AsyncSession, team: str, now, since, title: str) -> tuple[bytes, str]:
    rows = [r for r in await svc.attendance_report(session, now, since) if r["games"]]
    return build(rows, team, title, texts.fmt_date(now)), f"Посещаемость {team} {now:%d.%m.%Y}.pdf"


async def balance_pdf(session: AsyncSession, team: str, now, since, title: str) -> tuple[bytes, str]:
    rows = await svc.leaderboard(session, now, since)
    return build_balance(rows, team, title, texts.fmt_date(now)), f"Итог {team} {now:%d.%m.%Y}.pdf"


def _pdf(title: str) -> FPDF:
    pdf = FPDF(orientation="P", unit="mm", format="A4")
    pdf.set_auto_page_break(auto=True, margin=12)
    pdf.add_font("DejaVu", "", str(FONTS / "DejaVuSans.ttf"))
    pdf.add_font("DejaVu", "B", str(FONTS / "DejaVuSans-Bold.ttf"))
    pdf.set_title(title)
    pdf.add_page()
    return pdf


def _fit(pdf: FPDF, text: str, width: float) -> str:
    while pdf.get_string_width(text) > width - 2 and len(text) > 3:
        text = text[:-2] + "…"
    return text


def build_balance(rows: list[dict], team: str, period_title: str, date_label: str) -> bytes:
    """«Итог» для команды: помощь − минусы, места как в приложении (при равенстве — одно место)."""
    rows = sorted(rows, key=lambda r: (-r["balance"], -r["duties"], r["name"]))
    pdf = _pdf(f"Итог — {team}")
    pdf.set_font("DejaVu", "B", 16)
    pdf.cell(0, 9, f"Итог команды — {team}", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("DejaVu", "", 10)
    pdf.set_text_color(90, 90, 90)
    pdf.cell(0, 6, f"{period_title.capitalize()} · на {date_label}", new_x="LMARGIN", new_y="NEXT")
    pdf.multi_cell(0, 5, "Итог = помощь − минусы: +1 за каждую выполненную обязанность, −1 за каждый минус. "
                         "Спасибо всем, кто помогает команде!", new_x="LMARGIN", new_y="NEXT")
    pdf.set_text_color(0, 0, 0)
    pdf.ln(3)
    cols = [("Место", 16), ("Игрок", 74), ("Помощь", 24), ("Минусы", 24), ("Посещения", 24), ("Итог", 24)]

    def header() -> None:
        pdf.set_font("DejaVu", "B", 9)
        pdf.set_fill_color(235, 235, 235)
        for name, w in cols:
            pdf.cell(w, 8, name, border=1, align="C", fill=True)
        pdf.ln()

    header()
    rank, prev = 0, None
    medals = {1: (212, 175, 55), 2: (170, 170, 170), 3: (176, 112, 60)}
    for i, r in enumerate(rows):
        if r["balance"] != prev:  # одинаковый итог — одно место, как в приложении
            rank, prev = i + 1, r["balance"]
        if pdf.get_y() > 280:
            pdf.add_page()
            header()
        fill = i % 2 == 1
        pdf.set_fill_color(248, 248, 248)
        top = rank <= 3 and r["balance"] > 0
        pdf.set_font("DejaVu", "B" if top else "", 10)
        if top:
            pdf.set_text_color(*medals[rank])
        pdf.cell(cols[0][1], 8, str(rank), border=1, align="C", fill=fill)
        pdf.set_text_color(0, 0, 0)
        pdf.cell(cols[1][1], 8, _fit(pdf, r["name"], cols[1][1]), border=1, fill=fill)
        pdf.set_font("DejaVu", "", 10)
        pdf.cell(cols[2][1], 8, f"+{r['duties']}" if r["duties"] else "0", border=1, align="C", fill=fill)
        pdf.cell(cols[3][1], 8, f"−{r['minuses']}" if r["minuses"] else "0", border=1, align="C", fill=fill)
        pdf.cell(cols[4][1], 8, str(r["games"]), border=1, align="C", fill=fill)
        b = r["balance"]
        pdf.set_text_color(*((190, 40, 40) if b < 0 else (30, 130, 50) if b > 0 else (0, 0, 0)))
        pdf.set_font("DejaVu", "B", 11)
        pdf.cell(cols[5][1], 8, f"+{b}" if b > 0 else f"−{-b}" if b < 0 else "0", border=1, align="C", fill=fill)
        pdf.set_text_color(0, 0, 0)
        pdf.ln()
    if not rows:
        pdf.set_font("DejaVu", "", 10)
        pdf.cell(0, 8, "Пока пусто — сыграйте первую игру.", new_x="LMARGIN", new_y="NEXT")
    return bytes(pdf.output())


def build(rows: list[dict], team: str, period_title: str, date_label: str) -> bytes:
    pdf = _pdf(f"Посещаемость — {team}")

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
        name = _fit(pdf, r["name"] + (" ✍" if r["manual"] else ""), cols[0][1])
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
