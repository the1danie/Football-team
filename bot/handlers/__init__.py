from aiogram import Router

from bot.handlers import admin, common, gear, group, late, result, stats, swap


def build_router() -> Router:
    router = Router()
    # Порядок важен: состояния ввода (профиль, создание игры) — раньше общих кнопок.
    router.include_routers(late.router, gear.router, result.router, common.router, admin.router, swap.router, stats.router, group.router)
    return router
