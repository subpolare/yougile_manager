from __future__ import annotations

from unittest.mock import AsyncMock
from datetime import datetime

from sqlalchemy import func, select

from app.db import replace_binding
from app.daily_greeting import DailyGreetingProvider
from app.models import DailyDispatch
from app.scheduler import run_daily_dispatch
from app.task_service import DigestService
from app.yougile import WorkspaceSnapshot


async def test_scheduler_uses_mocked_telegram_and_is_idempotent(database, monkeypatch) -> None:
    class Clock:
        @staticmethod
        def now(tz):
            return datetime(2026, 9, 9, 12, tzinfo=tz)

    monkeypatch.setattr("app.scheduler.datetime", Clock)
    _, factory = database
    await replace_binding(
        factory,
        chat_id=-1001,
        project_id="project-a",
        project_number=1,
        project_title="#1 A",
    )
    snapshot = WorkspaceSnapshot(boards=(), columns=(), tasks=(), users=())
    yougile = AsyncMock()
    yougile.fetch_workspace.return_value = snapshot
    bot = AsyncMock()
    service = DigestService(yougile, {}, DailyGreetingProvider(factory))

    await run_daily_dispatch(
        bot=bot,
        session_factory=factory,
        yougile=yougile,
        digest_service=service,
    )
    await run_daily_dispatch(
        bot=bot,
        session_factory=factory,
        yougile=yougile,
        digest_service=service,
    )

    bot.send_message.assert_awaited_once()
    async with factory() as session:
        assert await session.scalar(select(func.count()).select_from(DailyDispatch)) == 1
