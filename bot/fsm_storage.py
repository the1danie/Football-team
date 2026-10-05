"""Хранилище состояний aiogram FSM в БД (нужно для serverless, полезно и на сервере)."""

from contextlib import asynccontextmanager
from contextvars import ContextVar
from typing import Any, AsyncIterator, Mapping

from aiogram.fsm.state import State
from aiogram.fsm.storage.base import BaseStorage, StateType, StorageKey
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bot.models import FsmRecord

# Сессия текущего апдейта (ставит DbSessionMiddleware): состояние диалога пишется
# в той же транзакции, что и данные хендлера.
current_session: ContextVar[AsyncSession | None] = ContextVar("current_session", default=None)


def _key(key: StorageKey) -> str:
    return ":".join(
        str(part)
        for part in (
            key.bot_id, key.chat_id, key.user_id, key.thread_id or "", key.business_connection_id or "", key.destiny,
        )
    )


class SqlStorage(BaseStorage):
    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession]):
        self.sessionmaker = sessionmaker

    @asynccontextmanager
    async def _session(self) -> AsyncIterator[tuple[AsyncSession, bool]]:
        shared = current_session.get()
        if shared is not None:
            yield shared, False
            return
        async with self.sessionmaker() as session:
            yield session, True

    async def _save(self, key: StorageKey, **values: Any) -> None:
        async with self._session() as (session, own):
            row = await session.get(FsmRecord, _key(key))
            state = values.get("state", row.state if row else None)
            data = values.get("data", row.data if row else {})
            if state is None and not data:
                if row is not None:
                    await session.delete(row)
            elif row is None:
                session.add(FsmRecord(key=_key(key), state=state, data=data))
            else:
                row.state, row.data = state, data
            if own:
                await session.commit()
            else:
                await session.flush()

    async def _load(self, key: StorageKey) -> FsmRecord | None:
        async with self._session() as (session, _own):
            return await session.get(FsmRecord, _key(key))

    async def set_state(self, key: StorageKey, state: StateType = None) -> None:
        await self._save(key, state=state.state if isinstance(state, State) else state)

    async def get_state(self, key: StorageKey) -> str | None:
        row = await self._load(key)
        return row.state if row else None

    async def set_data(self, key: StorageKey, data: Mapping[str, Any]) -> None:
        await self._save(key, data=dict(data))

    async def get_data(self, key: StorageKey) -> dict[str, Any]:
        row = await self._load(key)
        return dict(row.data) if row and row.data else {}

    async def close(self) -> None:
        pass
