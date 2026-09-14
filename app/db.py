from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import date

from sqlalchemy import delete, or_, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.models import Base, ChatProjectBinding, DailyDispatch, PersonalDigestSubscription


logger = logging.getLogger(__name__)
SessionFactory = async_sessionmaker[AsyncSession]


def create_engine_and_session(url: str) -> tuple[AsyncEngine, SessionFactory]:
    engine = create_async_engine(url, pool_pre_ping=True, pool_recycle=1800)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


async def initialize_database(
    engine: AsyncEngine, *, attempts: int = 30, retry_seconds: float = 2.0
) -> None:
    for attempt in range(1, attempts + 1):
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            logger.info("Database tables are ready")
            return
        except SQLAlchemyError as exc:
            logger.warning(
                "Database startup attempt %s/%s failed (%s)",
                attempt,
                attempts,
                type(exc).__name__,
            )
            if attempt == attempts:
                raise
            await asyncio.sleep(retry_seconds)


async def get_binding(session_factory: SessionFactory, chat_id: int) -> ChatProjectBinding | None:
    async with session_factory() as session:
        return await session.get(ChatProjectBinding, chat_id)


async def list_bindings(session_factory: SessionFactory) -> list[ChatProjectBinding]:
    async with session_factory() as session:
        result = await session.scalars(
            select(ChatProjectBinding).order_by(ChatProjectBinding.telegram_chat_id)
        )
        return list(result)


async def replace_binding(
    session_factory: SessionFactory,
    *,
    chat_id: int,
    project_id: str,
    project_number: int | None,
    project_title: str,
) -> None:
    """Atomically replace both sides of the chat <-> project one-to-one relation."""
    async with session_factory() as session, session.begin():
        rows = list(
            await session.scalars(
                select(ChatProjectBinding)
                .where(
                    or_(
                        ChatProjectBinding.telegram_chat_id == chat_id,
                        ChatProjectBinding.yougile_project_id == project_id,
                    )
                )
                .with_for_update()
            )
        )
        current_chat = next((row for row in rows if row.telegram_chat_id == chat_id), None)
        project_owner = next(
            (row for row in rows if row.yougile_project_id == project_id), None
        )

        if project_owner is not None and project_owner.telegram_chat_id != chat_id:
            await session.delete(project_owner)
            # Free the unique project key before updating the current chat row.
            await session.flush()

        if current_chat is None:
            session.add(
                ChatProjectBinding(
                    telegram_chat_id=chat_id,
                    yougile_project_id=project_id,
                    project_number=project_number,
                    project_title=project_title,
                )
            )
        else:
            current_chat.yougile_project_id = project_id
            current_chat.project_number = project_number
            current_chat.project_title = project_title
        await session.flush()


async def dispatch_once(
    session_factory: SessionFactory,
    *,
    chat_id: int,
    expected_project_id: str,
    dispatch_date: date,
    sender: Callable[[], Awaitable[None]],
) -> bool:
    """Send under a per-binding lock, recording only a completely successful send."""
    async with session_factory() as session, session.begin():
        binding = await session.scalar(
            select(ChatProjectBinding)
            .where(ChatProjectBinding.telegram_chat_id == chat_id)
            .with_for_update()
        )
        if binding is None or binding.yougile_project_id != expected_project_id:
            return False

        already_sent = await session.scalar(
            select(DailyDispatch.telegram_chat_id).where(
                DailyDispatch.telegram_chat_id == chat_id,
                DailyDispatch.dispatch_date == dispatch_date,
            )
        )
        if already_sent is not None:
            return False

        await sender()
        session.add(DailyDispatch(telegram_chat_id=chat_id, dispatch_date=dispatch_date))
        await session.flush()
        return True


async def remove_dispatch(
    session_factory: SessionFactory, *, chat_id: int, dispatch_date: date
) -> None:
    """Test/support helper; not used by normal dispatch flow."""
    async with session_factory() as session, session.begin():
        await session.execute(
            delete(DailyDispatch).where(
                DailyDispatch.telegram_chat_id == chat_id,
                DailyDispatch.dispatch_date == dispatch_date,
            )
        )


async def list_personal_subscriptions(session_factory: SessionFactory) -> list[PersonalDigestSubscription]:
    async with session_factory() as session:
        return list(await session.scalars(select(PersonalDigestSubscription).where(
            PersonalDigestSubscription.enabled.is_(True)
        ).order_by(PersonalDigestSubscription.telegram_user_id)))


async def dispatch_personal_once(
    session_factory: SessionFactory, *, telegram_id: int, expected_user_id: str,
    dispatch_date: date, sender: Callable[[], Awaitable[None]],
) -> bool:
    async with session_factory() as session, session.begin():
        subscription = await session.scalar(select(PersonalDigestSubscription).where(
            PersonalDigestSubscription.telegram_user_id == telegram_id
        ).with_for_update())
        if (subscription is None or not subscription.enabled
                or subscription.yougile_user_id != expected_user_id):
            return False
        # Private user IDs are positive; group chat IDs are negative. Both use
        # the same destination/date ledger without changing existing records.
        if await session.get(DailyDispatch, (telegram_id, dispatch_date)) is not None:
            return False
        await sender()
        session.add(DailyDispatch(telegram_chat_id=telegram_id, dispatch_date=dispatch_date))
        await session.flush()
        return True
