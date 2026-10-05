from aiogram import Router

from bot.handlers import admin, common, group, stats, swap


def build_router() -> Router:
    router = Router()
    # Порядок важен: состояния ввода (профиль, создание игры) — раньше общих кнопок.
    router.include_routers(common.router, admin.router, swap.router, stats.router, group.router)
    return router
