from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot import actions, keyboards, notifier, texts
from bot.config import config
from bot.models import User
from bot.services import games as svc

router = Router()
router.message.filter(F.chat.type == "private")

MENU_BUTTONS = {
    texts.BTN_CREATE, texts.BTN_CURRENT, texts.BTN_PARTICIPANTS, texts.BTN_DISTRIBUTE,
    texts.BTN_EDIT, texts.BTN_CANCEL, texts.BTN_STATS, texts.BTN_PROFILE, texts.BTN_SWAP,
}

HELP = (
    "Я распределяю обязанности перед играми и тренировками.\n\n"
    "• Отмечайся под сообщением об игре в чате команды: ✅ Буду / ❌ Не буду / 🤔 Пока не знаю.\n"
    "• После окончания сбора я распределю обязанности между теми, кто придёт, — "
    "в первую очередь тем, кто делал это реже остальных.\n"
    "• Мячи достаются только игрокам с машиной — укажи её в профиле.\n"
    "• Не можешь выполнить обязанность — нажми «🔄 Поменяться».\n"
    "• Передумал идти — нажми «❌ Не буду», обязанность перейдёт другому.\n\n"
    "Команды: /menu, /profile, /stats, /help"
)
ADMIN_HELP = (
    "\n\n<b>Администратору</b>\n"
    "/bindchat — выполнить в чате команды, чтобы бот публиковал туда игры\n"
    "/duties — список обязанностей\n"
    "/add_duty 🩹 Аптечка — добавить обязанность (добавьте слово «машина», если нужна машина)\n"
    "/toggle_duty &lt;id&gt; — включить/выключить обязанность"
)


class ProfileStates(StatesGroup):
    name = State()
    car = State()
    edit_name = State()


async def show_menu(message: Message, text: str = "Главное меню 👇") -> None:
    await message.answer(text, reply_markup=keyboards.main_menu(config.is_admin(message.chat.id)))


async def require_profile(message: Message, session: AsyncSession, state: FSMContext) -> User | None:
    """Вернуть пользователя или начать заполнение профиля."""
    user = await svc.get_user_by_tg(session, message.chat.id)
    if user is not None and user.profile_completed:
        return user
    await state.set_state(ProfileStates.name)
    await message.answer("Сначала заполним профиль.\n\nКак тебя зовут?\n\nНапример: <i>Даниял</i>")
    return None


# ----------------------------------------------------------------- /start


@router.message(CommandStart())
async def start(message: Message, command: CommandObject, session: AsyncSession, state: FSMContext, bot: Bot):
    await state.clear()
    payload = command.args or ""
    if payload:
        await state.update_data(after_profile=payload)
    user = await require_profile(message, session, state)
    if user is None:
        return
    if payload.startswith("swap_"):
        from bot.handlers.swap import open_swap  # избегаем циклического импорта

        await open_swap(message, session, user, int(payload.removeprefix("swap_")))
        return
    await show_menu(message, f"Привет, {texts.h(user.name)}! ⚽\n\n{HELP}")


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
async def profile_car(cb: CallbackQuery, session: AsyncSession, state: FSMContext):
    data = await state.get_data()
    user = await svc.get_or_create_user(session, cb.from_user.id, data["name"])
    user.name = data["name"]
    user.has_car = cb.data == "car:1"
    user.profile_completed = True
    await state.clear()
    await cb.message.edit_text(texts.profile_text(user) + "\n\n✅ Профиль сохранён.")
    await cb.answer()

    payload = data.get("after_profile", "")
    if payload.startswith("swap_"):
        from bot.handlers.swap import open_swap

        await open_swap(cb.message, session, user, int(payload.removeprefix("swap_")))
        return
    await show_menu(cb.message, HELP)


@router.message(Command("profile"))
@router.message(F.text == texts.BTN_PROFILE)
async def profile_show(message: Message, session: AsyncSession, state: FSMContext):
    await state.clear()
    user = await require_profile(message, session, state)
    if user:
        await message.answer(texts.profile_text(user), reply_markup=keyboards.profile_actions())


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
async def profile_edit_car(cb: CallbackQuery):
    await cb.message.answer("Есть ли у тебя машина?", reply_markup=keyboards.car_choice("pcar"))
    await cb.answer()


@router.callback_query(F.data.startswith("pcar:"))
async def profile_edit_car_save(cb: CallbackQuery, session: AsyncSession, bot: Bot):
    user = await svc.get_user_by_tg(session, cb.from_user.id)
    if user is None:
        await cb.answer("Сначала нажми /start", show_alert=True)
        return
    had_car = user.has_car
    user.has_car = cb.data == "pcar:1"
    await cb.message.edit_text(texts.profile_text(user) + "\n\n✅ Сохранено.")
    await cb.answer()

    if had_car and not user.has_car:
        moved = await svc.drop_car_duties(session, user, config.now())
        for game, reassigned in moved:
            await notifier.refresh_game(bot, session, game)
            await notifier.announce_reassignments(bot, game, reassigned)


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
