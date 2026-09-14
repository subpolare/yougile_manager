"""Opt-in checks against real MySQL 8.4; never call Telegram or OpenAI.

Set REMINDER_MYSQL_TEST_URL to an isolated MySQL database. All owned test rows
are additionally cleaned in finally. This suite deliberately tests independent
engines: local asyncio locks cannot hide incorrect MySQL locking.
"""
import asyncio
import os
import secrets
from datetime import date, timedelta
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy import delete, select, text

from app.db import create_engine_and_session, initialize_database
from app.models import VoiceReminder, ReminderDelivery, ReminderEditSession
from app.reminder_service import ReminderService, utcnow

pytestmark = pytest.mark.skipif(not os.getenv("REMINDER_MYSQL_TEST_URL"),
                                reason="Set REMINDER_MYSQL_TEST_URL for isolated MySQL integration tests")


@pytest_asyncio.fixture
async def mysql_services():
    url = os.environ["REMINDER_MYSQL_TEST_URL"]
    engine, factory = create_engine_and_session(url)
    other_engine, other_factory = create_engine_and_session(url)
    await initialize_database(engine, attempts=1)
    uid = 8_000_000_000_000_000 + secrets.randbelow(1_000_000_000)
    counter = iter(range(100, 1000))
    bot = AsyncMock()
    bot.send_message.side_effect = lambda **_: NS(message_id=next(counter))
    errors = NS(report=AsyncMock())
    first = ReminderService(factory, bot, None, errors)
    second = ReminderService(other_factory, bot, None, errors)
    async with first.transaction(uid) as session:
        now = utcnow()
        reminder = VoiceReminder(telegram_user_id=uid, original_voice_chat_id=uid,
                                 original_voice_message_id=1, transcript="Тестовая речь",
                                 text="Тестовое напоминание", created_at=now, updated_at=now)
        session.add(reminder)
        await session.flush()
        rid = reminder.id
    try:
        yield first, second, uid, rid
        errors.report.assert_not_awaited()
    finally:
        async with factory() as session, session.begin():
            await session.execute(delete(VoiceReminder).where(VoiceReminder.telegram_user_id == uid))
        await engine.dispose()
        await other_engine.dispose()


async def test_mysql_concurrent_schedule_manual_and_real_cascade(mysql_services):
    first, second, uid, rid = mysql_services
    day = date(2026, 9, 14)
    await asyncio.gather(first.deliver(uid, scheduled_date=day), second.deliver(uid, scheduled_date=day))
    assert first.bot.send_message.await_count == 1
    await first.deliver(uid)
    await second.deliver(uid)
    async with first.transaction(uid) as session:
        deliveries = list(await session.scalars(select(ReminderDelivery).where(ReminderDelivery.reminder_id == rid)))
        assert len(deliveries) == 3
        session.add(ReminderEditSession(telegram_user_id=uid, reminder_id=rid,
                    prompt_chat_id=uid, prompt_message_id=9999, expires_at=utcnow() + timedelta(minutes=15), created_at=utcnow()))
    msg = NS(chat=NS(id=uid), from_user=NS(id=uid), reply_to_message=NS(message_id=100), answer=AsyncMock())
    await second.done(msg)
    async with first.session_factory() as session:
        assert await session.get(VoiceReminder, rid) is None
        assert await session.get(ReminderEditSession, uid) is None
        assert not list(await session.scalars(select(ReminderDelivery).where(ReminderDelivery.reminder_id == rid)))


async def test_mysql_timeout_text_race_across_process_connections(mysql_services):
    first, second, uid, rid = mysql_services
    async with first.transaction(uid) as session:
        session.add(ReminderEditSession(telegram_user_id=uid, reminder_id=rid,
                    prompt_chat_id=uid, prompt_message_id=9999, expires_at=utcnow() - timedelta(seconds=1), created_at=utcnow()))
    msg = NS(chat=NS(id=uid, type="private"), from_user=NS(id=uid), text="replacement", answer=AsyncMock())
    await asyncio.gather(first.cleanup_expired(), second.replace_text(msg))
    async with first.session_factory() as session:
        assert (await session.get(VoiceReminder, rid)).text == "Тестовое напоминание"
        assert await session.get(ReminderEditSession, uid) is None
    assert first.bot.send_message.await_count == 1


async def test_mysql_delete_commits_before_send_lookup(mysql_services):
    first, second, uid, rid = mysql_services
    async with first.transaction(uid) as session:
        await session.delete(await session.get(VoiceReminder, rid))
    await second.deliver(uid)
    first.bot.send_message.assert_not_awaited()


async def test_mysql_rollback_releases_named_lock(mysql_services):
    first, second, uid, rid = mysql_services
    with pytest.raises(RuntimeError):
        async with first.transaction(uid) as session:
            reminder = await session.get(VoiceReminder, rid)
            reminder.text = "must roll back"
            raise RuntimeError("test failure")
    async with second.transaction(uid) as session:
        assert (await session.get(VoiceReminder, rid)).text == "Тестовое напоминание"
    async with first.session_factory() as session:
        assert await session.scalar(text("SELECT IS_FREE_LOCK(:name)"), {"name": f"voice-reminder:{uid}"}) == 1
