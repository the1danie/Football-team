from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot import actions, keyboards, notifier, operations, texts
from bot.config import config
from bot.models import User, UserStatus
from bot.services import games as svc

router = Router()
router.message.filter(F.chat.type == "private")

MENU_BUTTONS = {
    texts.BTN_CREATE, texts.BTN_CURRENT, texts.BTN_PARTICIPANTS, texts.BTN_DISTRIBUTE,
    texts.BTN_EDIT, texts.BTN_CANCEL, texts.BTN_STATS, texts.BTN_PROFILE, texts.BTN_SWAP, texts.BTN_PLAYERS,
    texts.BTN_WEB,
}

HELP = (
    "Я распределяю обязанности перед играми и тренировками.\n\n"
    "• Отмечайся под сообщением об игре в чате команды: ✅ Буду / ❌ Не буду / 🤔 Пока не знаю.\n"
    "• После окончания сбора я распределю обязанности между теми, кто придёт, — "
    "в первую очередь тем, кто делал это реже остальных.\n"
    "• Мячи достаются только игрокам с машиной — укажи её в профиле.\n"
    "• Не можешь выполнить обязанность — нажми «🔄 Поменяться».\n"
    "• Передумал идти — нажми «❌ Не буду», обязанность перейдёт другому.\n"
    "• Не ответил на опрос до закрытия сбора — минус. Пока есть минусы, обязанности достаются "
    "тебе первым; каждая выполненная обязанность списывает один минус.\n\n"
    "Нет Telegram-приложения под рукой или оно не открывается? /web — личная ссылка для браузера.\n\n"
    "Команды: /menu, /profile, /stats, /web, /help"
)
ADMIN_HELP = (
    "\n\n<b>Администратору</b>\n"
    "/bindchat — если команда в Telegram-группе: выполнить там, бот будет публиковать игры в неё. "
    "Без этого (команда в WhatsApp) бот присылает вам готовые тексты с кнопкой «📤 Отправить в WhatsApp»\n"
    "/duties — список обязанностей\n"
    "/add_duty 🩹 Аптечка — добавить обязанность (добавьте слово «машина», если нужна машина)\n"
    "/toggle_duty &lt;id&gt; — включить/выключить обязанность\n"
    "/invite — инструкция для команды в WhatsApp (как подключиться к боту и сайту)\n"
    "🗂 Игроки (/players) — заявки новых игроков, машина, имя, состав, удаление\n"
    "/penalties — минусы за неответы, снятие минуса\n"
    "/log — журнал действий админов (только главному)"
)


class ProfileStates(StatesGroup):
    name = State()
    car = State()
    edit_name = State()


async def show_menu(message: Message, text: str = "Главное меню 👇") -> None:
    await message.answer(text, reply_markup=keyboards.main_menu(config.is_admin(message.chat.id)))


async def require_profile(message: Message, session: AsyncSession, state: FSMContext) -> User | None:
    """Вернуть пользователя или начать заполнение профиля.

    Имя берём из профиля Telegram, спрашиваем только про машину.
    """
    user = await svc.get_user_by_tg(session, message.chat.id)
    if user is not None and user.profile_completed:
        return user
    name = await svc.name_from_telegram(session, message.from_user) if message.from_user else ""
    if not name:  # в Telegram имя пустое (бывает у служебных аккаунтов) — спросим
        await state.set_state(ProfileStates.name)
        await message.answer("Сначала заполним профиль.\n\nКак тебя зовут?\n\nНапример: <i>Даниял</i>")
        return None
    await state.update_data(name=name)
    await state.set_state(ProfileStates.car)
    await message.answer(
        f"Привет, {texts.h(name)}! 👋\n\nЕсть ли у тебя машина?\n"
        "<i>Это нужно, чтобы мячи доставались только тем, кто на машине.</i>\n\n"
        "Имя я взял из Telegram — поменять можно в «👤 Мой профиль».",
        reply_markup=keyboards.car_choice("car"),
    )
    return None


# ----------------------------------------------------------------- /start


@router.message(CommandStart())
async def start(message: Message, command: CommandObject, session: AsyncSession, state: FSMContext, bot: Bot):
    await state.clear()
    payload = command.args or ""
    if payload.startswith("link_"):
        await link_from_invite(message, session, bot, payload)
        return
    if payload:
        await state.update_data(after_profile=payload)
    user = await require_profile(message, session, state)
    if user is None:
        return
    if await open_payload(message, session, user, payload):
        return
    await show_menu(message, f"Привет, {texts.h(user.name)}! ⚽\n\n{HELP}")
    app = keyboards.with_web_button(keyboards.with_app_button(None), user)
    if app is not None:
        await message.answer(
            "📱 Удобнее всего — в приложении: игры, обязанности, рейтинг. "
            "Оно открывается и кнопкой «Открыть» рядом с полем ввода.\n"
            "🌐 Не пользуешься Telegram каждый день? Есть сайт — кнопка ниже (личная ссылка, не пересылай).",
            reply_markup=app,
        )


async def link_from_invite(message: Message, session: AsyncSession, bot: Bot, payload: str) -> None:
    """Ссылка-приглашение для игрока, которого админ добавил вручную: привязать этот Telegram к нему."""
    from bot.weblink import parse_link_payload

    user_id = parse_link_payload(payload)
    manual = await session.get(User, user_id) if user_id else None
    if manual is None or manual.status != UserStatus.APPROVED:
        await message.answer("Ссылка-приглашение недействительна. Попроси у админа новую.")
        return
    if not manual.is_manual:
        if manual.telegram_id == message.chat.id:
            await show_menu(message, f"Ты уже в команде, {texts.h(manual.name)} ⚽")
        else:
            await message.answer("По этой ссылке уже зашёл другой человек. Если это ошибка — напиши админу.")
        return
    try:
        await operations.link_account(
            bot, session, manual, message.chat.id, message.from_user.username if message.from_user else None
        )
    except operations.OpError as e:
        await message.answer(str(e))
        return
    await notifier.notify_admins(bot, f"🔗 {texts.h(manual.name)} зашёл в бота по приглашению — Telegram привязан.")


async def open_payload(message: Message, session: AsyncSession, user: User, payload: str) -> bool:
    """Ссылки вида t.me/бот?start=…: game_<id> — опрос по игре, swap_<id> — обмен."""
    kind, _, raw_id = payload.partition("_")
    if not raw_id.isdigit():
        return False
    if kind == "swap":
        from bot.handlers.swap import open_swap  # избегаем циклического импорта

        await open_swap(message, session, user, int(raw_id))
        return True
    if kind == "game":
        game = await svc.get_game(session, int(raw_id))
        if game is None:
            return False
        await show_menu(message, f"Привет, {texts.h(user.name)}! ⚽")
        text, markup = await actions.game_card(session, game, user, config.is_admin(message.chat.id))
        await message.answer(text, reply_markup=markup)
        return True
    return False


@router.message(Command("help"))
async def help_cmd(message: Message):
    text = HELP + (ADMIN_HELP if config.is_admin(message.chat.id) else "")
    await message.answer(text)


@router.message(Command("menu"))
async def menu_cmd(message: Message, state: FSMContext):
    await state.clear()
    await show_menu(message)


# ----------------------------------------------------------------- профиль


@router.message(ProfileStates.name, F.text)
async def profile_name(message: Message, state: FSMContext):
    name = message.text.strip()
    if name in MENU_BUTTONS or name.startswith("/") or not (1 <= len(name) <= 64):
        await message.answer("Напиши имя текстом (до 64 символов).")
        return
    await state.update_data(name=name)
    await state.set_state(ProfileStates.car)
    await message.answer("Есть ли у тебя машина?", reply_markup=keyboards.car_choice("car"))


@router.callback_query(ProfileStates.car, F.data.startswith("car:"))
async def profile_car(cb: CallbackQuery, session: AsyncSession, state: FSMContext, bot: Bot):
    data = await state.get_data()
    user = await operations.complete_registration(
        bot, session, cb.from_user.id, data["name"], cb.from_user.username, cb.data == "car:1"
    )
    await state.clear()
    await cb.answer()

    if not user.is_approved:
        await cb.message.edit_text(
            texts.profile_text(user) + "\n\n✅ Профиль сохранён.\n\n"
            "⏳ Заявка отправлена администратору. Как только он подтвердит, что ты из команды, "
            "придёт сообщение — и можно будет отмечаться на игры."
        )
        return

    await cb.message.edit_text(texts.profile_text(user) + "\n\n✅ Профиль сохранён.")

    if await open_payload(cb.message, session, user, data.get("after_profile", "")):
        return
    await show_menu(cb.message, HELP)


@router.message(Command("profile"))
@router.message(F.text == texts.BTN_PROFILE)
async def profile_show(message: Message, session: AsyncSession, state: FSMContext):
    await state.clear()
    user = await require_profile(message, session, state)
    if user:
        minuses = (await svc.open_penalty_points(session, [user.id])).get(user.id, 0)
        await message.answer(texts.profile_text(user, minuses), reply_markup=keyboards.profile_actions())


@router.callback_query(F.data == "prof:name")
async def profile_edit_name(cb: CallbackQuery, state: FSMContext):
    await state.set_state(ProfileStates.edit_name)
    await cb.message.answer("Напиши новое имя:")
    await cb.answer()


@router.message(ProfileStates.edit_name, F.text)
async def profile_edit_name_save(message: Message, session: AsyncSession, state: FSMContext):
    name = message.text.strip()
    if name in MENU_BUTTONS or name.startswith("/") or not (1 <= len(name) <= 64):
        await message.answer("Напиши имя текстом (до 64 символов).")
        return
    user = await svc.get_user_by_tg(session, message.chat.id)
    if user is None:
        await state.set_state(ProfileStates.name)
        await profile_name(message, state)
        return
    user.name = name
    await state.clear()
    await message.answer(texts.profile_text(user) + "\n\n✅ Имя изменено.", reply_markup=keyboards.profile_actions())


@router.callback_query(F.data == "prof:car")
async def profile_edit_car(cb: CallbackQuery, session: AsyncSession):
    user = await svc.get_user_by_tg(session, cb.from_user.id)
    if user is not None and user.car_locked and not config.is_admin(cb.from_user.id):
        await cb.answer("Наличие машины отметил администратор — изменить может только он.", show_alert=True)
        return
    await cb.message.answer("Есть ли у тебя машина?", reply_markup=keyboards.car_choice("pcar"))
    await cb.answer()


@router.callback_query(F.data.startswith("pcar:"))
async def profile_edit_car_save(cb: CallbackQuery, session: AsyncSession, bot: Bot):
    user = await svc.get_user_by_tg(session, cb.from_user.id)
    if user is None:
        await cb.answer("Сначала нажми /start", show_alert=True)
        return
    try:
        await operations.set_own_car(bot, session, user, cb.data == "pcar:1")
    except operations.OpError as e:
        await cb.answer(str(e), show_alert=True)
        return
    await cb.message.edit_text(texts.profile_text(user) + "\n\n✅ Сохранено.")
    await cb.answer()


# ----------------------------------------------------------------- текущая игра


@router.message(Command("game"))
@router.message(F.text == texts.BTN_CURRENT)
async def current_game(message: Message, session: AsyncSession, state: FSMContext):
    await state.clear()
    user = await require_profile(message, session, state)
    if user is None:
        return
    games = await svc.upcoming_games(session, config.now())
    if not games:
        await message.answer("Запланированных игр нет.")
        return
    is_admin = config.is_admin(message.chat.id)
    for game in games:
        text, markup = await actions.game_card(session, game, user, is_admin)
        await message.answer(text, reply_markup=markup)


@router.callback_query(F.data == "noop")
async def noop(cb: CallbackQuery):
    await cb.message.edit_reply_markup(reply_markup=None)
    await cb.answer("Ок")


# ----------------------------------------------------------------- веб-версия


async def send_web_link(message: Message, session: AsyncSession, user: User) -> None:
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    from bot.miniapp_api import web_link

    link = web_link(user)
    if link is None:
        await message.answer("Веб-версия доступна, когда бот работает на Vercel (нужен публичный адрес).")
        return
    await message.answer(
        "🌐 <b>Твоя личная ссылка для браузера</b> (Chrome, Safari — без Telegram):\n\n"
        f"<code>{texts.h(link)}</code>\n\n"
        "Открой её — и ты в приложении команды: игры, «Буду / Не буду», обязанности, статистика. "
        "Браузер запомнит вход; можно добавить на главный экран.\n\n"
        "⚠️ Не пересылай ссылку: по ней входят от твоего имени. Если она попала не тому — "
        "в приложении: Профиль → «Выйти на всех устройствах».",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🌐 Открыть", url=link)]]),
    )


@router.message(Command("web"))
@router.message(F.text == texts.BTN_WEB)
async def web_cmd(message: Message, session: AsyncSession, state: FSMContext):
    await state.clear()
    user = await require_profile(message, session, state)
    if user is not None:
        await send_web_link(message, session, user)


@router.callback_query(F.data == "prof:web")
async def web_from_profile(cb: CallbackQuery, session: AsyncSession):
    await cb.answer()
    user = await svc.get_user_by_tg(session, cb.from_user.id)
    if user is not None:
        await send_web_link(cb.message, session, user)
