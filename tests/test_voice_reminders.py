from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select

from app.config import Settings
from app.error_reporter import ErrorReporter, Sanitizer
from app.models import VoiceReminder, ReminderDelivery, ReminderEditSession, ErrorAdminTarget
from app.openai_service import (OpenAIService, ReminderExtraction, InvalidExtraction,
                                UnusableVoice, EXTRACTION_INSTRUCTIONS)
from app.personal_identity import PersonalIdentity, UNKNOWN
from app.reminder_handlers import create_reminder_router
from app.reminder_service import (ReminderService, utcnow, DONE, FORGOTTEN, TIMEOUT,
                                  EDIT_PROMPT, VOICE_FAILURE)
from app.scheduler import run_daily_dispatch, create_scheduler
from app.telegram_handlers import create_router
from app.yougile import WorkspaceSnapshot, YouGileAPIError
from test_telegram_handlers import command_handler, RecordingActionContext

UID = "11111111-1111-1111-1111-111111111111"
TODAY = date(2026, 9, 14)


def settings(**kwargs):
    return Settings(_env_file=None, yougile_company_id="test", yougile_api_key="test",
                    telegram_bot_token="test", **kwargs)


def message(user=42, text=None, reply=None):
    return NS(chat=NS(id=user, type="private"), from_user=NS(id=user, username="some_user"),
              message_id=7, voice=NS(file_id="voice"), text=text, reply_to_message=reply,
              message_thread_id=None, answer=AsyncMock())


def callback(reminder, action, msg_id=10, user=42):
    return NS(data=f"vr:{action}:{reminder.id}", from_user=NS(id=user),
              message=NS(chat=NS(id=user, type="private"), message_id=msg_id), answer=AsyncMock())


@pytest.fixture
def service(database, monkeypatch):
    count = iter(range(10, 1000))
    bot = AsyncMock()
    bot.send_message.side_effect = lambda **_: NS(message_id=next(count))
    ai = NS(transcribe_voice=AsyncMock(return_value="Так, завтра проверить монтаж"),
            extract_reminder=AsyncMock(return_value="Проверить монтаж 15 сентября"))
    errors = NS(report=AsyncMock())
    monkeypatch.setattr("app.reminder_handlers.ChatActionSender.typing",
                        lambda **_: RecordingActionContext([]))
    return ReminderService(database[1], bot, ai, errors)


async def rows(service, model):
    async with service.session_factory() as session:
        return list(await session.scalars(select(model)))


async def create(service):
    return await service.create_from_voice(message())


async def start_edit(service, reminder):
    await service.callback(callback(reminder, "edit"))
    return (await rows(service, ReminderEditSession))[0]


async def expire(service):
    async with service.session_factory() as session, session.begin():
        edit = await session.get(ReminderEditSession, 42)
        edit.expires_at = utcnow() - timedelta(seconds=1)


def sdk_client():
    return NS(audio=NS(transcriptions=NS(create=AsyncMock(return_value=NS(text="Русский текст")))),
              responses=NS(parse=AsyncMock(return_value=NS(output_parsed=ReminderExtraction(reminder=" Сделать "))),
                           create=AsyncMock(return_value=NS(output_text="Сбой запроса."))))


async def test_openai_models_typed_output_reasoning_and_moscow_date(monkeypatch, tmp_path):
    class Clock:
        @staticmethod
        def now(tz):
            assert str(tz) == "Europe/Moscow"
            return datetime(2026, 9, 14, 1, tzinfo=tz)
    monkeypatch.setattr("app.openai_service.datetime", Clock)
    client = sdk_client()
    ai = OpenAIService(settings(), client)
    path = tmp_path / "voice.ogg"
    assert await ai.transcribe_voice(path) == "Русский текст"
    client.audio.transcriptions.create.assert_awaited_once_with(
        model="gpt-transcribe", file=path, languages=["ru"], response_format="json")
    assert await ai.extract_reminder("завтра сделать") == "Сделать"
    kw = client.responses.parse.await_args.kwargs
    assert kw["model"] == "gpt-5.6-terra"
    assert kw["reasoning"] == {"effort": "none"} and kw["store"] is False
    assert kw["text_format"] is ReminderExtraction
    assert all(s in kw["instructions"] for s in ["2026-09-14", "14 сентября 2026", "понедельник", "завтра", "через неделю"])
    assert kw["input"] == [{"role": "user", "content": "завтра сделать"}]
    await ai.explain_error("safe context")
    kw = client.responses.create.await_args.kwargs
    assert kw["model"] == "gpt-5.6-sol"
    assert kw["reasoning"] == {"effort": "none"} and kw["store"] is False
    assert "tools" not in kw


async def test_configured_models():
    client = sdk_client()
    ai = OpenAIService(settings(openai_reminder_model="configured-extraction", openai_error_model="configured-error"), client)
    await ai.extract_reminder("test")
    await ai.explain_error("safe")
    assert client.responses.parse.await_args.kwargs["model"] == "configured-extraction"
    assert client.responses.create.await_args.kwargs["model"] == "configured-error"
    for call in (client.responses.parse, client.responses.create):
        assert call.await_args.kwargs["reasoning"] == {"effort": "none"}


@pytest.mark.parametrize("value", ["", " ", "x" * 1501, None])
def test_invalid_extraction_validation(value):
    with pytest.raises(ValueError):
        ReminderExtraction(reminder=value)


async def test_missing_structured_output():
    client = sdk_client()
    client.responses.parse.return_value = NS(output_parsed=None)
    with pytest.raises(InvalidExtraction):
        await OpenAIService(settings(), client).extract_reminder("test")


@pytest.mark.parametrize("text", ["", " ", "..."])
async def test_empty_transcription_expected(text, tmp_path):
    client = sdk_client()
    client.audio.transcriptions.create.return_value = NS(text=text)
    with pytest.raises(UnusableVoice):
        await OpenAIService(settings(), client).transcribe_voice(tmp_path / "a.ogg")


@pytest.mark.parametrize("failure", [None, "transcribe_voice", "extract_reminder", "download"])
async def test_audio_cleanup_and_no_partial_persistence(service, failure):
    paths = []
    async def download(_, *, destination):
        paths.append(destination)
        destination.write_bytes(b"test ogg")
        if failure == "download":
            raise RuntimeError("download failed")
    service.bot.download.side_effect = download
    if failure in {"transcribe_voice", "extract_reminder"}:
        getattr(service.openai, failure).side_effect = RuntimeError("request failed")
    if failure:
        with pytest.raises(RuntimeError):
            await create(service)
        assert await rows(service, VoiceReminder) == []
    else:
        reminder = await create(service)
        assert reminder.transcript == "Так, завтра проверить монтаж"
        assert reminder.text == "Проверить монтаж 15 сентября"
        kw = service.bot.send_message.await_args.kwargs
        assert kw["reply_parameters"].message_id == 7 and kw["parse_mode"] is None
        assert kw["text"] == f"Я понял так:\n\n«{reminder.text}»"
        assert [b.text for b in kw["reply_markup"].inline_keyboard[0]] == ["✅ Ок", "✍️ Изменить", "❌ Удалить"]
        assert all(reminder.text not in b.callback_data for b in kw["reply_markup"].inline_keyboard[0])
    assert paths and all(not path.exists() and path.suffix == ".ogg" for path in paths)


@pytest.mark.parametrize("known", [True, False])
async def test_voice_handler_authorization_before_openai(service, known):
    identity = PersonalIdentity(service.session_factory, {UID: "@some_user"} if known else {})
    router = create_reminder_router(service, identity)
    msg = message()
    await command_handler(router, "voice")(msg)
    service.bot.send_chat_action.assert_awaited_once_with(chat_id=42, action="typing")
    if known:
        assert len(await rows(service, VoiceReminder)) == 1
    else:
        msg.answer.assert_awaited_once_with(UNKNOWN)
        service.openai.transcribe_voice.assert_not_awaited()
        service.errors.report.assert_not_awaited()


async def test_voice_api_failure_reports_no_reminder(service):
    service.openai.extract_reminder.side_effect = RuntimeError("failed")
    router = create_reminder_router(service, PersonalIdentity(service.session_factory, {UID: "@some_user"}))
    msg = message()
    await command_handler(router, "voice")(msg)
    msg.answer.assert_awaited_once_with(VOICE_FAILURE)
    assert await rows(service, VoiceReminder) == []
    service.errors.report.assert_awaited_once()


async def test_ok_readonly_and_delete_exact_reply(service):
    reminder = await create(service)
    service.bot.send_message.reset_mock()
    ok = callback(reminder, "ok")
    await service.callback(ok)
    ok.answer.assert_awaited_once_with()
    service.bot.send_message.assert_not_awaited()
    saved = (await rows(service, VoiceReminder))[0]
    assert saved.updated_at == reminder.updated_at
    assert saved.transcript == reminder.transcript
    await service.callback(callback(reminder, "delete"))
    assert await rows(service, VoiceReminder) == []
    kw = service.bot.send_message.await_args.kwargs
    assert kw["text"] == FORGOTTEN and kw["reply_parameters"].message_id == 7
    await service.callback(callback(reminder, "delete"))
    assert service.bot.send_message.await_count == 1
    service.errors.report.assert_not_awaited()


async def test_edit_persist_replace_and_original_transcript(service):
    reminder = await create(service)
    edit = await start_edit(service, reminder)
    assert abs((edit.expires_at - utcnow()).total_seconds() - 900) < 3
    kw = service.bot.send_message.await_args.kwargs
    assert kw["text"] == EDIT_PROMPT
    assert [[b.text for b in r] for r in kw["reply_markup"].inline_keyboard] == [
        ["✅ Оставить, как было"], ["❌ Отменить напоминание и удалить его"]]
    service.bot.edit_message_reply_markup.assert_any_await(chat_id=42, message_id=10, reply_markup=None)
    # Reconstruct service to simulate a restart; all edit state comes from the DB.
    restarted = ReminderService(service.session_factory, service.bot, service.openai, service.errors)
    msg = message(text="  Мой <текст> & формулировка  ")
    assert await restarted.replace_text(msg)
    saved = (await rows(service, VoiceReminder))[0]
    assert saved.text == "Мой <текст> & формулировка" and saved.transcript == reminder.transcript
    assert await rows(service, ReminderEditSession) == []
    assert msg.answer.await_args.kwargs["parse_mode"] is None
    assert service.openai.extract_reminder.await_count == 1


@pytest.mark.parametrize("value", ["/start", "/stop", "/task", "/done", "/other", " /task"])
async def test_commands_not_replacement(service, value):
    reminder = await create(service)
    await start_edit(service, reminder)
    assert not await service.replace_text(message(text=value))
    assert (await rows(service, VoiceReminder))[0].text == reminder.text
    assert len(await rows(service, ReminderEditSession)) == 1


@pytest.mark.parametrize("value", ["", "  ", "x" * 1501])
async def test_invalid_edit_keeps_session(service, value):
    reminder = await create(service)
    await start_edit(service, reminder)
    await service.replace_text(message(text=value))
    assert len(await rows(service, ReminderEditSession)) == 1
    assert (await rows(service, VoiceReminder))[0].text == reminder.text


async def test_second_edit_closes_previous_and_stale_cancel_cannot_delete(service):
    first, second = await create(service), await create(service)
    old = await start_edit(service, first)
    new = await start_edit(service, second)
    assert len(await rows(service, ReminderEditSession)) == 1 and new.reminder_id == second.id
    await service.callback(callback(first, "cancel", old.prompt_message_id))
    assert len(await rows(service, VoiceReminder)) == 2
    service.bot.edit_message_reply_markup.assert_any_await(chat_id=42, message_id=old.prompt_message_id, reply_markup=None)
    await service.callback(callback(second, "keep", new.prompt_message_id))
    assert await rows(service, ReminderEditSession) == []
    assert (await rows(service, VoiceReminder))[1].text == second.text


async def test_edit_cancel_cascades_and_replies_original(service):
    reminder = await create(service)
    await service.deliver(42)
    edit = await start_edit(service, reminder)
    await service.callback(callback(reminder, "cancel", edit.prompt_message_id))
    for model in (VoiceReminder, ReminderDelivery, ReminderEditSession):
        assert await rows(service, model) == []
    kw = service.bot.send_message.await_args.kwargs
    assert kw["text"] == FORGOTTEN and kw["reply_parameters"].message_id == 7


async def test_timeout_restart_preserves_and_notifies_once(service):
    reminder = await create(service)
    edit = await start_edit(service, reminder)
    await expire(service)
    restarted = ReminderService(service.session_factory, service.bot, service.openai, service.errors)
    await restarted.cleanup_expired()
    await restarted.cleanup_expired()
    assert await rows(service, ReminderEditSession) == []
    assert (await rows(service, VoiceReminder))[0].text == reminder.text
    assert sum(c.kwargs.get("text") == TIMEOUT for c in service.bot.send_message.await_args_list) == 1
    service.bot.edit_message_reply_markup.assert_any_await(chat_id=42, message_id=edit.prompt_message_id, reply_markup=None)


@pytest.mark.parametrize("action", ["text", "delete", "keep"])
async def test_timeout_races_one_winner(service, action):
    reminder = await create(service)
    edit = await start_edit(service, reminder)
    await expire(service)
    contender = (service.replace_text(message(text="new")) if action == "text" else
                 service.callback(callback(reminder, action, edit.prompt_message_id)))
    await asyncio.gather(service.cleanup_expired(), contender)
    assert await rows(service, ReminderEditSession) == []
    saved = await rows(service, VoiceReminder)
    assert saved == [] if action == "delete" else saved[0].text == reminder.text
    assert sum(c.kwargs.get("text") == TIMEOUT for c in service.bot.send_message.await_args_list) <= 1


@pytest.mark.parametrize("copy", [0, 1, 2])
async def test_done_any_old_delivery_cascades_all_personal_data(service, copy):
    reminder = await create(service)
    service.bot.send_message.side_effect = [NS(message_id=i) for i in (100, 200, 300, 400, 500)]
    for _ in range(3):
        await service.deliver(42)
    deliveries = await rows(service, ReminderDelivery)
    assert [d.telegram_message_id for d in deliveries] == [100, 200, 300]
    assert {d.reminder_id for d in deliveries} == {reminder.id}
    await start_edit(service, reminder)
    msg = message(reply=NS(message_id=deliveries[copy].telegram_message_id))
    await service.done(msg)
    for model in (VoiceReminder, ReminderDelivery, ReminderEditSession):
        assert await rows(service, model) == []
    kw = service.bot.send_message.await_args.kwargs
    assert kw["text"] == DONE and kw["reply_parameters"].message_id == deliveries[copy].telegram_message_id


@pytest.mark.parametrize("reply,user", [(None, 42), (999, 42), (100, 43)])
async def test_done_validation_no_admin(service, reply, user):
    await create(service)
    service.bot.send_message.side_effect = [NS(message_id=100)]
    await service.deliver(42)
    msg = message(user=user, reply=NS(message_id=reply) if reply else None)
    await service.done(msg)
    assert len(await rows(service, VoiceReminder)) == 1
    msg.answer.assert_awaited_once()
    service.errors.report.assert_not_awaited()


@pytest.mark.parametrize("action", ["ok", "edit", "delete", "cancel", "keep"])
async def test_callback_ownership(service, action):
    reminder = await create(service)
    await service.callback(callback(reminder, action, user=43))
    assert len(await rows(service, VoiceReminder)) == 1
    service.errors.report.assert_not_awaited()


async def test_scheduled_idempotency_manual_independence_order(service):
    first, second = await create(service), await create(service)
    await service.deliver(42)
    await asyncio.gather(service.deliver(42, scheduled_date=TODAY), service.deliver(42, scheduled_date=TODAY))
    await service.deliver(42)
    deliveries = await rows(service, ReminderDelivery)
    assert len(deliveries) == 6
    assert sum(d.scheduled_date is not None for d in deliveries) == 2
    assert [d.reminder_id for d in deliveries] == [first.id, second.id] * 3


async def test_delete_before_send_lookup_and_done_delete_race(service):
    reminder = await create(service)
    await service.deliver(42)
    delivery = (await rows(service, ReminderDelivery))[0]
    await asyncio.gather(service.done(message(reply=NS(message_id=delivery.telegram_message_id))),
                         service.callback(callback(reminder, "delete")))
    service.bot.send_message.reset_mock()
    await service.deliver(42)
    service.bot.send_message.assert_not_awaited()
    assert await rows(service, ReminderDelivery) == []


def freeze(monkeypatch, day):
    class Clock:
        @staticmethod
        def now(tz):
            return datetime.combine(day, datetime.min.time(), tzinfo=tz).replace(hour=12)
    monkeypatch.setattr("app.scheduler.datetime", Clock)


@pytest.mark.parametrize("subscription", ["enabled", "stopped", "never"])
@pytest.mark.parametrize("yg_failure", [False, True])
async def test_scheduler_independent_of_subscriptions_and_yougile(service, monkeypatch, subscription, yg_failure):
    freeze(monkeypatch, TODAY)
    identity = PersonalIdentity(service.session_factory, {UID: "@some_user"})
    if subscription != "never":
        await identity.resolve(42, "some_user", action="start")
    if subscription == "stopped":
        await identity.resolve(42, "some_user", action="stop")
    await create(service)
    service.bot.send_message.reset_mock()
    yougile = AsyncMock()
    yougile.fetch_workspace.return_value = WorkspaceSnapshot(boards=(), columns=(), tasks=(), users=())
    if yg_failure:
        yougile.fetch_workspace.side_effect = YouGileAPIError("down")
    personal = NS(build=AsyncMock(return_value=(["digest"], None)), task_index=lambda snapshot: {})
    kwargs = dict(bot=service.bot, session_factory=service.session_factory, yougile=yougile,
                  digest_service=None, personal_service=personal, reminder_service=service, error_reporter=service.errors)
    await run_daily_dispatch(**kwargs)
    await run_daily_dispatch(**kwargs)
    texts = [c.kwargs["text"] for c in service.bot.send_message.await_args_list]
    expected = (["digest"] if subscription == "enabled" and not yg_failure else []) + ["📝 Проверить монтаж 15 сентября"]
    assert texts == expected
    assert len(await rows(service, ReminderDelivery)) == 1


@pytest.mark.parametrize("day", [12, 13])
async def test_no_automatic_weekend(service, monkeypatch, day):
    freeze(monkeypatch, date(2026, 9, day))
    await create(service)
    service.bot.send_message.reset_mock()
    await run_daily_dispatch(bot=service.bot, session_factory=service.session_factory,
                             yougile=AsyncMock(), digest_service=None, reminder_service=service)
    service.bot.send_message.assert_not_awaited()


@pytest.mark.parametrize("day", [12, 13, 14])
@pytest.mark.parametrize("yg_failure", [False, True])
async def test_private_task_after_stop_repeat_weekends_failure(service, monkeypatch, day, yg_failure):
    freeze(monkeypatch, date(2026, 9, day))
    monkeypatch.setattr("app.telegram_handlers.ChatActionSender.typing", lambda **_: RecordingActionContext([]))
    identity = PersonalIdentity(service.session_factory, {UID: "@some_user"})
    await identity.resolve(42, "some_user", action="start")
    await identity.resolve(42, "some_user", action="stop")
    await create(service)
    events = []
    async def send(**kw):
        events.append(kw["text"])
        return NS(message_id=len(events) + 100)
    service.bot.send_message.side_effect = send
    personal = NS(build=AsyncMock(return_value=(["digest"], None)), task_index=lambda snapshot: {})
    if yg_failure:
        personal.build.side_effect = YouGileAPIError("down")
    router = create_router(session_factory=service.session_factory, yougile=AsyncMock(), digest_service=None,
                           personal_service=personal, personal_identity=identity, reminder_service=service,
                           error_reporter=service.errors)
    msg = message()
    msg.answer.side_effect = lambda text: events.append(text)
    for _ in range(2):
        await command_handler(router, "task")(msg, service.bot)
    assert len(events) == 4
    assert events[1].startswith("📝") and events[3].startswith("📝")
    assert (events[0].startswith("Не удалось") if yg_failure else events[0] == "digest")
    assert all(d.scheduled_date is None for d in await rows(service, ReminderDelivery))


async def test_cleanup_schedule_is_persistent(service):
    scheduler = create_scheduler(bot=service.bot, session_factory=service.session_factory,
                                 yougile=None, digest_service=None, reminder_service=service)
    assert scheduler.get_job("expire-reminder-edits").trigger.interval.total_seconds() == 60
    trigger = scheduler.get_job("daily-yougile-digest").trigger
    assert str(trigger.timezone) == "Europe/Moscow"


async def test_admin_sol_sanitized_and_fallback(database, caplog):
    bot = AsyncMock()
    ai = NS(explain_error=AsyncMock(return_value="Не удалось создать напоминание."))
    reporter = ErrorReporter(bot, database[1], ai, admin_id=999, secrets=["yg-secret"])
    private = 'transcript=личная речь reminder.text=личная задача Authorization: Bearer abc sk-secret yg-secret'
    original = RuntimeError(private)
    await reporter.report(original, component="voice_reminders", operation="transcribe_voice",
                          transcript="личная речь", reminder_text="личная задача", telegram_user_id=42)
    context = ai.explain_error.await_args.args[0]
    assert "RuntimeError" in context and "42" in context
    for secret in ["личная речь", "личная задача", "abc", "sk-secret", "yg-secret"]:
        assert secret not in context and secret not in caplog.text
    assert bot.send_message.await_args.kwargs["chat_id"] == 999
    ai.explain_error.side_effect = ConnectionError(private)
    await reporter.report(original, component="voice_reminders", operation="transcribe_voice")
    alert = bot.send_message.await_args.kwargs["text"]
    assert "RuntimeError" in alert and "ConnectionError" in alert and "Sol тоже не ответила" in alert
    assert ai.explain_error.await_count == 2
    bot.send_message.side_effect = RuntimeError(private)
    await reporter.report(original, component="voice_reminders", operation="transcribe_voice")
    assert ai.explain_error.await_count == 3
    assert "Admin notification failed" in caplog.text
    assert "личная речь" not in caplog.text


def test_sanitizer_patterns():
    value = 'Bearer abc 123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZ_123456 sk-proj-abc mysql+asyncmy://user:pass@host/db?token=xyz YOUGILE_API_KEY=abc'
    result = Sanitizer().clean(value)
    for secret in ["abc", "123456789", "pass", "xyz", "ABCDEFGHIJKLMNOPQRSTUVWXYZ"]:
        assert secret not in result


async def test_admin_bootstrap_no_fake_subscription(database):
    from app.models import PersonalDigestSubscription
    ai = NS(explain_error=AsyncMock())
    reporter = ErrorReporter(AsyncMock(), database[1], ai)
    await reporter.observe_admin(NS(id=999, username="SubPolare"))
    restarted = ErrorReporter(AsyncMock(), database[1], ai)
    assert await restarted.resolve_admin() == 999
    await restarted.observe_admin(NS(id=1000, username="subpolare"))
    assert await restarted.resolve_admin() == 999
    async with database[1]() as session:
        assert await session.scalar(select(func.count()).select_from(PersonalDigestSubscription)) == 0


async def test_admin_uses_existing_disabled_subscription(database):
    identity = PersonalIdentity(database[1], {UID: "@subpolare"})
    await identity.resolve(999, "subpolare", action="start")
    await identity.resolve(999, "subpolare", action="stop")
    reporter = ErrorReporter(AsyncMock(), database[1], NS())
    assert await reporter.resolve_admin() == 999


async def test_missing_admin_loud_no_recursion(database, caplog):
    ai = NS(explain_error=AsyncMock(return_value="Сбой."))
    bot = AsyncMock()
    reporter = ErrorReporter(bot, database[1], ai)
    await reporter.startup_check()
    await reporter.report(RuntimeError("private"), component="test", operation="test")
    assert "ERROR ADMIN UNRESOLVED" in caplog.text
    ai.explain_error.assert_awaited_once()
    bot.send_message.assert_not_awaited()


async def test_filters_group_voice_and_commands(service):
    from aiogram.types import Message, Chat, User, Voice
    router = create_reminder_router(service, PersonalIdentity(service.session_factory, {}))
    def event(chat_type, text=None, voice=False):
        return Message(message_id=1, date=datetime.now(), chat=Chat(id=42, type=chat_type),
                       from_user=User(id=42, is_bot=False, first_name="test"), text=text,
                       voice=Voice(file_id="x", file_unique_id="y", duration=1) if voice else None)
    voice_handler = next(h for h in router.message.handlers if h.callback.__name__ == "voice")
    edit_handler = next(h for h in router.message.handlers if h.callback.__name__ == "replacement_text")
    done_handler = next(h for h in router.message.handlers if h.callback.__name__ == "done")
    assert (await voice_handler.check(event("private", voice=True)))[0]
    assert not (await voice_handler.check(event("supergroup", voice=True)))[0]
    for cmd in ["/start", "/stop", "/task", "/done", "/unknown"]:
        assert not (await edit_handler.check(event("private", text=cmd)))[0]
    assert (await edit_handler.check(event("private", text="new text")))[0]
    assert not (await done_handler.check(event("supergroup", text="/done")))[0]


async def test_failure_to_delete_never_confirms(service, monkeypatch):
    from contextlib import asynccontextmanager
    reminder = await create(service)
    service.bot.send_message.reset_mock()
    original = service.transaction
    @asynccontextmanager
    async def rollback(user_id):
        async with original(user_id) as session:
            yield session
            raise RuntimeError("simulated commit failure")
    monkeypatch.setattr(service, "transaction", rollback)
    with pytest.raises(RuntimeError):
        await service.callback(callback(reminder, "delete"))
    assert len(await rows(service, VoiceReminder)) == 1
    service.bot.send_message.assert_not_awaited()


async def test_edit_commit_failure_removes_new_keyboard(service, monkeypatch):
    from contextlib import asynccontextmanager
    reminder = await create(service)
    original = service.transaction
    @asynccontextmanager
    async def rollback(user_id):
        async with original(user_id) as session:
            yield session
            raise RuntimeError("simulated commit failure")
    monkeypatch.setattr(service, "transaction", rollback)
    with pytest.raises(RuntimeError):
        await start_edit(service, reminder)
    assert await rows(service, ReminderEditSession) == []
    service.bot.edit_message_reply_markup.assert_any_await(chat_id=42, message_id=11, reply_markup=None)


async def test_sdk_rejection_and_unusable_voice_report_classification(service):
    service.openai.transcribe_voice.side_effect = UnusableVoice("empty")
    msg = message()
    router = create_reminder_router(service, PersonalIdentity(service.session_factory, {UID: "@some_user"}))
    await command_handler(router, "voice")(msg)
    msg.answer.assert_awaited_with(VOICE_FAILURE)
    service.errors.report.assert_not_awaited()
    assert await rows(service, VoiceReminder) == []


async def test_reporter_sdk_sol_failure_is_sanitized_terminal_fallback(database, caplog):
    client = sdk_client()
    client.responses.create.side_effect = ConnectionError('transcript=private-body token=secret-value')
    bot = AsyncMock()
    reporter = ErrorReporter(bot, database[1], OpenAIService(settings(), client), admin_id=999)
    await reporter.report(RuntimeError('reminder=private-reminder password=db-secret'),
                          component='voice_task_drafts', operation='draft_input')
    client.responses.create.assert_awaited_once()
    options = client.responses.create.await_args.kwargs
    assert options['model'] == 'gpt-5.6-sol'
    assert options['reasoning'] == {'effort': 'none'} and options['store'] is False
    bot.send_message.assert_awaited_once()
    alert = bot.send_message.await_args.kwargs['text']
    assert 'Sol тоже не ответила' in alert and 'Terra' not in alert
    for secret in ('private-body', 'secret-value', 'private-reminder', 'db-secret'):
        assert secret not in options['input'] and secret not in alert and secret not in caplog.text
