import asyncio
from datetime import date, datetime, timedelta
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.models import VoiceReminder, VoiceTaskDraft, VoiceTaskDraftSession, ReminderEditSession, ReminderDelivery
from app.openai_service import OpenAIService, YouGileTaskExtraction, InvalidExtraction
from app.personal_identity import PersonalIdentity, UNKNOWN
from app.reminder_handlers import create_reminder_router
from app.reminder_service import ReminderService, FORGOTTEN, utcnow
from app.voice_task_drafts import (VoiceTaskDraftService, human_task, MISSING_DEADLINE,
                                   INVALID_DATE, EDIT_PROMPT, TIMEOUT, STALE, YOUGILE_FAILURE)
from app.yougile import YouGileAPIError
from test_voice_reminders import service, message, rows, settings, sdk_client, UID
from test_telegram_handlers import command_handler

TITLE = "Собрать референсы для обложки"
DEADLINE = date(2026, 9, 17)


@pytest.fixture
def drafts(service):
    identity = PersonalIdentity(service.session_factory, {UID: "@some_user", "other": "@another_user"},
                                sasha_tg="@some_user")
    service.openai.extract_yougile_task = AsyncMock(
        return_value=YouGileTaskExtraction(title=TITLE, deadline=DEADLINE))
    yg = NS(create_sasha_task=AsyncMock(return_value="task-id"))
    drafts = VoiceTaskDraftService(service, identity, yg)
    service.task_drafts = drafts
    return drafts


def cb(draft, action, user=42):
    return NS(data=f"vd:{action}:{draft.id}", from_user=NS(id=user, username="some_user"),
              message=NS(chat=NS(id=user, type="private"), message_id=10), answer=AsyncMock())


async def create(drafts, *, missing=False):
    if missing:
        drafts.openai.extract_yougile_task.return_value = YouGileTaskExtraction(title=TITLE, deadline=None)
    return await drafts.create_from_voice(message())


async def input_session(drafts):
    return (await rows(drafts, VoiceTaskDraftSession))[0]


async def set_expired(drafts):
    async with drafts.transaction(42) as session:
        edit = await session.get(VoiceTaskDraftSession, 42)
        edit.expires_at = utcnow() - timedelta(seconds=1)


def buttons(bot):
    return [b.text for row in bot.send_message.await_args.kwargs["reply_markup"].inline_keyboard for b in row]


async def test_extractor_schema_moscow_reasoning_none(monkeypatch):
    class Clock:
        @staticmethod
        def now(tz):
            assert str(tz) == "Europe/Moscow"
            return datetime(2026, 9, 15, tzinfo=tz)
    monkeypatch.setattr("app.openai_service.datetime", Clock)
    client = sdk_client()
    client.responses.parse.return_value = NS(output_parsed=YouGileTaskExtraction(title=TITLE, deadline=DEADLINE))
    result = await OpenAIService(settings(), client).extract_yougile_task("До четверга собрать референсы для обложки")
    assert result.title == TITLE and result.deadline == DEADLINE
    kw = client.responses.parse.await_args.kwargs
    assert kw['model'] == 'gpt-5.6-luna' and kw['reasoning'] == {'effort': 'none'} and kw['store'] is False
    assert '2026-09-15' in kw['instructions'] and 'вторник' in kw['instructions']
    assert kw['text_format'] is YouGileTaskExtraction
    assert set(YouGileTaskExtraction.model_fields) == {'title', 'deadline'}
    assert 'tools' not in kw
    client.responses.parse.return_value = NS(output_parsed=None)
    with pytest.raises(InvalidExtraction):
        await OpenAIService(settings(), client).extract_yougile_task('test')


async def test_sasha_private_voice_new_flow_and_original_reply(drafts):
    router = create_reminder_router(drafts.reminders, drafts.identity)
    await command_handler(router, 'voice')(message())
    assert len(await rows(drafts, VoiceTaskDraft)) == 1
    assert not await rows(drafts, VoiceReminder)
    kw = drafts.bot.send_message.await_args.kwargs
    assert kw['text'] == f'Я понял так:\n\n«{TITLE} до 17 сентября»'
    assert kw['reply_parameters'].message_id == 7 and kw['parse_mode'] is None
    assert buttons(drafts.bot) == ['✅ В YouGile', '📝 Напомнить тут', '✍️ Изменить', '❌ Удалить']
    for row in kw['reply_markup'].inline_keyboard:
        assert all(b.callback_data.startswith('vd:') and len(b.callback_data.split(':')) == 3 for b in row)
    drafts.yougile.create_sasha_task.assert_not_awaited()
    drafts.openai.extract_reminder.assert_not_awaited()


@pytest.mark.parametrize('known', [True, False])
async def test_other_employee_or_unknown_unchanged(drafts, known):
    msg = message(user=43)
    msg.from_user.username = 'another_user' if known else 'unknown_user'
    await command_handler(create_reminder_router(drafts.reminders, drafts.identity), 'voice')(msg)
    assert not await rows(drafts, VoiceTaskDraft)
    drafts.openai.extract_yougile_task.assert_not_awaited()
    if known:
        assert len(await rows(drafts, VoiceReminder)) == 1
        assert [b.text for b in drafts.bot.send_message.await_args.kwargs['reply_markup'].inline_keyboard[0]] == ['✅ Ок', '✍️ Изменить', '❌ Удалить']
    else:
        msg.answer.assert_awaited_once_with(UNKNOWN)


async def test_bound_identity_survives_username_change_and_recycling(drafts):
    await drafts.identity.resolve(42, 'some_user', action='start')
    msg = message()
    msg.from_user.username = 'changed_name'
    assert await drafts.eligible(msg.from_user)
    assert not await drafts.eligible(NS(id=99, username='some_user'))
    assert await drafts.create_from_voice(msg)
    group = message()
    group.chat.type = 'supergroup'
    assert await drafts.create_from_voice(group) is None


async def test_missing_deadline_is_persistent_inactive_and_no_keyboard(drafts):
    before = utcnow()
    draft = await create(drafts, missing=True)
    edit = await input_session(drafts)
    assert edit.mode == 'deadline' and edit.draft_id == draft.id
    assert before + timedelta(minutes=15) <= edit.expires_at <= utcnow() + timedelta(minutes=15)
    assert drafts.bot.send_message.await_args.kwargs['text'] == MISSING_DEADLINE
    assert 'reply_markup' not in drafts.bot.send_message.await_args.kwargs
    assert not await rows(drafts, VoiceReminder)
    drafts.yougile.create_sasha_task.assert_not_awaited()
    drafts.bot.send_message.reset_mock()
    await drafts.reminders.deliver(42)
    drafts.bot.send_message.assert_not_awaited()


@pytest.mark.parametrize('value', ['19.1.2004', '19/01/2004', 'завтра', 'foo', '32.01.2004', ' 19.01.2004', '19.01.2004\n'])
async def test_invalid_manual_date_preserves_original_window(drafts, value):
    draft = await create(drafts, missing=True)
    old = await input_session(drafts)
    msg = message(text=value)
    assert await drafts.replace_text(msg)
    msg.answer.assert_awaited_once_with(INVALID_DATE)
    assert (await input_session(drafts)).expires_at == old.expires_at
    assert (await rows(drafts, VoiceTaskDraft))[0].deadline_date is None
    assert drafts.openai.extract_yougile_task.await_count == 1
    assert not await rows(drafts, VoiceReminder)
    drafts.yougile.create_sasha_task.assert_not_awaited()


async def test_valid_manual_date_after_restart_closes_session(drafts):
    draft = await create(drafts, missing=True)
    restarted = VoiceTaskDraftService(drafts.reminders, drafts.identity, drafts.yougile)
    assert await restarted.replace_text(message(text='19.01.2004'))
    row = (await rows(drafts, VoiceTaskDraft))[0]
    assert row.id == draft.id and row.deadline_date == date(2004, 1, 19)
    assert not await rows(drafts, VoiceTaskDraftSession)
    assert drafts.bot.send_message.await_args.kwargs['text'] == f'Я понял так:\n\n«{TITLE} до 19 января 2004»'
    assert len(buttons(drafts.bot)) == 4
    assert drafts.openai.extract_yougile_task.await_count == 1


@pytest.mark.parametrize('command', ['/task', '/start', '/stop', '/done', ' /task'])
async def test_commands_never_consumed(drafts, command):
    await create(drafts, missing=True)
    old = await input_session(drafts)
    assert not await drafts.replace_text(message(text=command))
    assert (await input_session(drafts)).expires_at == old.expires_at


async def test_success_double_callback_deletes_only_after_post(drafts):
    draft = await create(drafts)
    async def post(**kw):
        assert len(await rows(drafts, VoiceTaskDraft)) == 1
        assert kw == dict(title=TITLE, deadline=DEADLINE, idempotency_key=draft.idempotency_key)
        return 'created-task'
    drafts.yougile.create_sasha_task.side_effect = post
    first, second = cb(draft, 'yg'), cb(draft, 'yg')
    await asyncio.gather(drafts.callback(first), drafts.callback(second))
    drafts.yougile.create_sasha_task.assert_awaited_once()
    assert not await rows(drafts, VoiceTaskDraft)
    assert not await rows(drafts, VoiceTaskDraftSession)
    assert not await rows(drafts, VoiceReminder)
    drafts.bot.edit_message_reply_markup.assert_awaited()
    assert {first.answer.await_args.args[0], second.answer.await_args.args[0]} == {
        'Готово, задача создана в YouGile', STALE}
    drafts.reminders.errors.report.assert_not_awaited()


async def test_failed_post_preserves_data_and_key_retry_succeeds(drafts):
    draft = await create(drafts)
    drafts.yougile.create_sasha_task.side_effect = YouGileAPIError('sensitive body')
    tap = cb(draft, 'yg')
    await drafts.callback(tap)
    row = (await rows(drafts, VoiceTaskDraft))[0]
    assert row.transcript == draft.transcript and row.idempotency_key == draft.idempotency_key
    tap.answer.assert_awaited_once_with(YOUGILE_FAILURE)
    drafts.reminders.errors.report.assert_awaited_once()
    drafts.bot.edit_message_reply_markup.assert_not_awaited()
    drafts.yougile.create_sasha_task.side_effect = None
    await drafts.callback(cb(draft, 'yg'))
    assert not await rows(drafts, VoiceTaskDraft)
    assert len({c.kwargs['idempotency_key'] for c in drafts.yougile.create_sasha_task.await_args_list}) == 1


async def test_local_atomic_conversion_delivery_history_and_done(drafts):
    draft = await create(drafts)
    tap = cb(draft, 'local')
    await drafts.callback(tap)
    row = (await rows(drafts, VoiceReminder))[0]
    assert row.text == f'{TITLE} до 17 сентября' and row.transcript == draft.transcript
    assert (row.original_voice_chat_id, row.original_voice_message_id) == (42, 7)
    assert not await rows(drafts, VoiceTaskDraft)
    drafts.yougile.create_sasha_task.assert_not_awaited()
    await drafts.reminders.deliver(42)
    await drafts.reminders.deliver(42, scheduled_date=date(2026, 9, 15))
    await drafts.reminders.deliver(42, scheduled_date=date(2026, 9, 15))
    deliveries = await rows(drafts, ReminderDelivery)
    assert len(deliveries) == 2
    await drafts.reminders.done(message(reply=NS(message_id=deliveries[0].telegram_message_id)))
    assert not await rows(drafts, VoiceReminder)
    assert not await rows(drafts, ReminderDelivery)


async def test_delete_with_session_cascades_and_replies_to_voice(drafts):
    draft = await create(drafts)
    await drafts.callback(cb(draft, 'edit'))
    await drafts.callback(cb(draft, 'delete'))
    assert not await rows(drafts, VoiceTaskDraft)
    assert not await rows(drafts, VoiceTaskDraftSession)
    assert not await rows(drafts, VoiceReminder)
    kw = drafts.bot.send_message.await_args.kwargs
    assert kw['text'] == FORGOTTEN and kw['reply_parameters'].message_id == 7
    drafts.yougile.create_sasha_task.assert_not_awaited()


@pytest.mark.parametrize('action', ['yg', 'local', 'delete', 'edit'])
async def test_nonowner_and_stale_callbacks_safe(drafts, action):
    draft = await create(drafts)
    await drafts.callback(cb(draft, action, user=43))
    assert len(await rows(drafts, VoiceTaskDraft)) == 1
    await drafts.callback(cb(draft, 'delete'))
    tap = cb(draft, action)
    await drafts.callback(tap)
    tap.answer.assert_awaited_once_with(STALE)
    drafts.reminders.errors.report.assert_not_awaited()
    drafts.yougile.create_sasha_task.assert_not_awaited()


@pytest.mark.parametrize('deadline', [date(2027, 1, 19), None])
async def test_edit_extraction_updates_same_draft_preserves_transcript(drafts, deadline):
    draft = await create(drafts)
    await drafts.callback(cb(draft, 'edit'))
    edit = await input_session(drafts)
    assert edit.mode == 'edit' and edit.expires_at - edit.created_at == timedelta(minutes=15)
    assert drafts.bot.send_message.await_args.kwargs['text'] == EDIT_PROMPT
    drafts.openai.extract_yougile_task.return_value = YouGileTaskExtraction(title='Новая задача', deadline=deadline)
    await drafts.replace_text(message(text='Новая задача до 19 января 2027'))
    drafts.openai.extract_yougile_task.assert_awaited_with('Новая задача до 19 января 2027')
    row = (await rows(drafts, VoiceTaskDraft))[0]
    assert row.title == 'Новая задача' and row.deadline_date == deadline and row.transcript == draft.transcript
    if deadline:
        assert not await rows(drafts, VoiceTaskDraftSession)
        assert len(buttons(drafts.bot)) == 4
    else:
        assert (await input_session(drafts)).mode == 'deadline'
        assert drafts.bot.send_message.await_args.kwargs['text'] == MISSING_DEADLINE


async def test_edit_failure_keeps_old_result_and_session(drafts):
    draft = await create(drafts)
    await drafts.callback(cb(draft, 'edit'))
    old = await input_session(drafts)
    drafts.openai.extract_yougile_task.side_effect = RuntimeError('hidden raw text')
    await drafts.replace_text(message(text='correction'))
    row = (await rows(drafts, VoiceTaskDraft))[0]
    assert row.title == draft.title and row.deadline_date == draft.deadline_date
    assert (await input_session(drafts)).expires_at == old.expires_at
    drafts.reminders.errors.report.assert_awaited_once()


@pytest.mark.parametrize('race', ['text', 'delete', 'edit'])
async def test_timeout_restart_and_races_physically_delete(drafts, race):
    draft = await create(drafts, missing=True)
    await set_expired(drafts)
    restarted = VoiceTaskDraftService(drafts.reminders, drafts.identity, drafts.yougile)
    other = restarted.replace_text(message(text='19.01.2004')) if race == 'text' else restarted.callback(cb(draft, race))
    await asyncio.gather(drafts.reminders.cleanup_expired(), other)
    assert not await rows(drafts, VoiceTaskDraft)
    assert not await rows(drafts, VoiceTaskDraftSession)
    assert not await rows(drafts, VoiceReminder)
    texts = [c.kwargs['text'] for c in drafts.bot.send_message.await_args_list]
    assert texts.count(TIMEOUT) == 1
    drafts.yougile.create_sasha_task.assert_not_awaited()
    drafts.reminders.errors.report.assert_not_awaited()


async def test_edit_timeout_restores_complete_draft_ui(drafts):
    draft = await create(drafts)
    await drafts.callback(cb(draft, 'edit'))
    await set_expired(drafts)
    await drafts.cleanup_expired()
    assert len(await rows(drafts, VoiceTaskDraft)) == 1
    assert not await rows(drafts, VoiceTaskDraftSession)
    assert len(buttons(drafts.bot)) == 4


@pytest.mark.parametrize('incomplete', [False, True])
async def test_session_replacement_preserves_complete_cleans_incomplete(drafts, incomplete):
    first = await create(drafts, missing=incomplete)
    if not incomplete:
        await drafts.callback(cb(first, 'edit'))
    drafts.openai.extract_yougile_task.return_value = YouGileTaskExtraction(title=TITLE, deadline=DEADLINE)
    second = await create(drafts)
    await drafts.callback(cb(second, 'edit'))
    assert (await input_session(drafts)).draft_id == second.id
    assert {d.id for d in await rows(drafts, VoiceTaskDraft)} == ({second.id} if incomplete else {first.id, second.id})


async def test_one_text_session_across_local_and_draft_edits(drafts):
    local = await drafts.reminders.create_from_voice(message())
    from test_voice_reminders import callback
    await drafts.reminders.callback(callback(local, 'edit'))
    draft = await create(drafts, missing=True)
    assert not await rows(drafts, ReminderEditSession)
    await drafts.reminders.callback(callback(local, 'edit'))
    assert not await rows(drafts, VoiceTaskDraftSession)
    assert not await rows(drafts, VoiceTaskDraft)
    assert len(await rows(drafts, ReminderEditSession)) == 1


def test_human_date_calendar_year():
    row = NS(title=TITLE, deadline_date=date(2027, 1, 19))
    assert human_task(row, today=date(2026, 9, 15)).endswith('19 января 2027')
    assert human_task(row, today=date(2027, 9, 15)).endswith('19 января')


async def test_callback_answer_failure_after_post_does_not_restore_draft(drafts):
    draft = await create(drafts)
    tap = cb(draft, 'yg')
    tap.answer.side_effect = RuntimeError('Telegram unavailable')
    await drafts.callback(tap)
    assert not await rows(drafts, VoiceTaskDraft)
    assert tap.answer.await_count == 1
    assert tap.answer.await_args.args == ('Готово, задача создана в YouGile',)
    assert drafts.reminders.errors.report.await_args.kwargs['operation'] == 'draft_callback_ui'
    await drafts.callback(cb(draft, 'yg'))
    drafts.yougile.create_sasha_task.assert_awaited_once()


async def test_telegram_failure_during_edit_keeps_previous_draft(drafts):
    draft = await create(drafts)
    await drafts.callback(cb(draft, 'edit'))
    drafts.openai.extract_yougile_task.return_value = YouGileTaskExtraction(title='Changed', deadline=DEADLINE)
    drafts.bot.send_message.side_effect = RuntimeError('Telegram send failure')
    await drafts.replace_text(message(text='Changed tomorrow'))
    row = (await rows(drafts, VoiceTaskDraft))[0]
    assert row.title == TITLE and row.transcript == draft.transcript
    assert (await input_session(drafts)).mode == 'edit'
    drafts.reminders.errors.report.assert_awaited_once()


@pytest.mark.parametrize('case', ['missing-project', 'many-boards', 'double-project'])
async def test_real_resolver_configuration_errors_hit_reporter(drafts, case):
    from test_yougile_task_creation import API, client
    api = API(existing=False)
    if case == 'missing-project':
        api.projects = []
    elif case == 'double-project':
        api.projects.append(dict(api.projects[0], id='p2'))
    else:
        api.boards.append(dict(api.boards[0], id='b2'))
    yg, http, _ = client(api)
    drafts.yougile = yg
    draft = await create(drafts)
    async with http:
        tap = cb(draft, 'yg')
        await drafts.callback(tap)
    assert len(await rows(drafts, VoiceTaskDraft)) == 1 and not api.posts
    drafts.reminders.errors.report.assert_awaited_once()
    tap.answer.assert_awaited_once_with(YOUGILE_FAILURE)


async def test_group_voice_filter_unchanged_with_extension(drafts):
    from aiogram.types import Message, Chat, User, Voice
    router = create_reminder_router(drafts.reminders, drafts.identity)
    handler = next(h for h in router.message.handlers if h.callback.__name__ == 'voice')
    event = Message(message_id=1, date=datetime.now(), chat=Chat(id=-123,type='supergroup'),
                    from_user=User(id=42,is_bot=False,first_name='Test',username='some_user'),
                    voice=Voice(file_id='synthetic',file_unique_id='synthetic',duration=1))
    assert not (await handler.check(event))[0]
    assert not await rows(drafts, VoiceTaskDraft)


async def test_valid_input_wins_cleanup_race_before_expiration(drafts):
    draft = await create(drafts, missing=True)
    await asyncio.gather(drafts.replace_text(message(text='19.01.2004')), drafts.cleanup_expired())
    row = (await rows(drafts, VoiceTaskDraft))[0]
    assert row.id == draft.id and row.deadline_date == date(2004,1,19)
    assert not await rows(drafts, VoiceTaskDraftSession)
    drafts.reminders.errors.report.assert_not_awaited()
