"""Opt-in isolated MySQL tests; only synthetic data and mocked external services."""
import asyncio
import os
from datetime import date, timedelta
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import delete, select

from app.models import VoiceTaskDraft, VoiceTaskDraftSession
from app.openai_service import YouGileTaskExtraction
from app.personal_identity import PersonalIdentity
from app.reminder_service import utcnow
from app.voice_task_drafts import VoiceTaskDraftService, TIMEOUT
from test_mysql_reminders import mysql_services

pytestmark = pytest.mark.skipif(not os.getenv('REMINDER_MYSQL_TEST_URL'),
                                reason='Set REMINDER_MYSQL_TEST_URL for isolated MySQL integration tests')


def setup(first, second, employee="SASHA"):
    ai = NS(transcribe_voice=AsyncMock(return_value='Тестовая речь'),
            extract_yougile_task=AsyncMock(return_value=YouGileTaskExtraction(title='Тест', deadline=date(2026, 9, 17))))
    yg = NS(create_voice_task=AsyncMock(return_value='task-id'))
    services = []
    for reminder in [first, second]:
        reminder.openai = ai
        identity = PersonalIdentity(reminder.session_factory, {'test-yg': '@test_user'}, voice_task_employees={employee: '@test_user'})
        drafts = VoiceTaskDraftService(reminder, identity, yg)
        reminder.task_drafts = drafts
        services.append(drafts)
    return services


def message(uid, text=None):
    return NS(chat=NS(id=uid, type='private'), from_user=NS(id=uid, username='test_user'),
              message_id=50, voice=NS(file_id='synthetic'), text=text, answer=AsyncMock())


def callback(draft, action):
    uid=draft.telegram_user_id
    return NS(data=f'vd:{action}:{draft.id}', from_user=NS(id=uid, username='test_user'),
              message=NS(chat=NS(id=uid,type='private'),message_id=100),answer=AsyncMock())


async def clean(drafts, uid):
    async with drafts.transaction(uid) as session:
        await session.execute(delete(VoiceTaskDraft).where(VoiceTaskDraft.telegram_user_id==uid))


@pytest.mark.parametrize("employee", ["SASHA", "VLAD", "KOSTYA"])
async def test_mysql_two_engines_double_post_and_physical_cascade(mysql_services, employee):
    first, second, uid, _ = mysql_services
    a, b = setup(first, second, employee)
    try:
        draft = await a.create_from_voice(message(uid))
        await asyncio.gather(a.callback(callback(draft, 'yg')), b.callback(callback(draft, 'yg')))
        a.yougile.create_voice_task.assert_awaited_once()
        async with first.session_factory() as session:
            assert await session.get(VoiceTaskDraft, draft.id) is None
            assert await session.get(VoiceTaskDraftSession, uid) is None
    finally:
        await clean(a, uid)


@pytest.mark.parametrize('employee', ['SASHA', 'VLAD', 'KOSTYA'])
@pytest.mark.parametrize('action', ['text', 'delete', 'edit'])
async def test_mysql_restart_expiration_races(mysql_services, action, employee):
    first, second, uid, _ = mysql_services
    a, b = setup(first, second, employee)
    a.openai.extract_yougile_task.return_value = YouGileTaskExtraction(title='Тест',deadline=None)
    try:
        draft = await a.create_from_voice(message(uid))
        async with a.transaction(uid) as session:
            edit = await session.get(VoiceTaskDraftSession, uid)
            edit.expires_at = utcnow() - timedelta(seconds=1)
        other = b.replace_text(message(uid, '19.01.2004')) if action == 'text' else b.callback(callback(draft, action))
        await asyncio.gather(a.cleanup_expired(), other)
        async with first.session_factory() as session:
            assert await session.get(VoiceTaskDraft,draft.id) is None
            assert await session.get(VoiceTaskDraftSession,uid) is None
        assert sum(c.kwargs.get('text') == TIMEOUT for c in a.bot.send_message.await_args_list) == 1
        a.yougile.create_voice_task.assert_not_awaited()
    finally:
        await clean(a, uid)
