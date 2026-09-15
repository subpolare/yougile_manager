import asyncio
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select

from app.daily_greeting import DailyGreetingProvider
from app.db import dispatch_personal_once, replace_binding
from app.greetings import DUMB_GREETINGS
from app.models import DailyDigestGreeting, DailyDispatch
from app.personal_digest import PersonalDigestService
from app.personal_identity import PersonalIdentity
from app.scheduler import create_scheduler, run_daily_dispatch
from app.task_service import DigestService, MOSCOW_TZ
from app.yougile import WorkspaceSnapshot, YouGileAPIError
from test_telegram_handlers import command_handler


UID = "11111111-1111-1111-1111-111111111111"
TODAY = date(2026, 9, 9)


async def test_daily_greeting_persists_restarts_dates_and_concurrent_first_requests(database):
    provider = DailyGreetingProvider(database[1])
    greetings = await asyncio.gather(*[DailyGreetingProvider(database[1]).get(TODAY) for _ in range(12)])
    assert len(set(greetings)) == 1 and greetings[0] in DUMB_GREETINGS
    assert await provider.get(TODAY) == greetings[0]
    await database[0].dispose()
    assert await DailyGreetingProvider(database[1]).get(TODAY) == greetings[0]
    assert await provider.get(TODAY + timedelta(days=1)) in DUMB_GREETINGS
    async with database[1]() as session:
        assert await session.scalar(select(func.count()).select_from(DailyDigestGreeting)) == 2


def freeze_day(monkeypatch, value):
    class Clock(datetime):
        @staticmethod
        def now(tz):
            return datetime.combine(value, datetime.min.time(), tzinfo=tz).replace(hour=12)
    monkeypatch.setattr("app.scheduler.datetime", Clock)
    monkeypatch.setattr("app.task_service.datetime", Clock)
    monkeypatch.setattr("app.personal_digest.datetime", Clock)


async def setup_dispatch(database):
    factory = database[1]
    await replace_binding(factory, chat_id=-1001, project_id="p", project_number=7, project_title="#7 P")
    identity = PersonalIdentity(factory, {UID: "@some_user"})
    await identity.resolve(42, "some_user", action="start")
    client = AsyncMock()
    client.fetch_workspace.return_value = WorkspaceSnapshot((), (), (), ())
    provider = DailyGreetingProvider(factory)
    group = DigestService(client, {}, provider)
    personal = PersonalDigestService(client, provider)
    return dict(bot=AsyncMock(), session_factory=factory, yougile=client,
                digest_service=group, personal_service=personal)


@pytest.mark.parametrize("day", range(7, 14))
async def test_automatic_weekdays_only_and_one_shared_snapshot(database, monkeypatch, day):
    freeze_day(monkeypatch, date(2026, 9, day))
    kwargs = await setup_dispatch(database)
    await run_daily_dispatch(**kwargs)
    weekday = day < 12
    assert kwargs["yougile"].fetch_workspace.await_count == int(weekday)
    assert kwargs["bot"].send_message.await_count == (2 if weekday else 0)
    if weekday:
        calls = kwargs["bot"].send_message.await_args_list
        assert {call.kwargs["chat_id"] for call in calls} == {-1001, 42}
        assert len({call.kwargs["text"].splitlines()[0] for call in calls}) == 1


def test_cron_no_catch_up_and_moscow_noon_weekdays():
    scheduler = create_scheduler(bot=AsyncMock(), session_factory=None, yougile=AsyncMock(),
                                 digest_service=None)
    job = scheduler.get_job("daily-yougile-digest")
    assert str(job.trigger.timezone) == "Europe/Moscow"
    assert job.coalesce and job.max_instances == 1 and job.misfire_grace_time == 60
    start = datetime(2026, 9, 7, 0, tzinfo=MOSCOW_TZ)
    previous = None
    for _ in range(15):
        next_run = job.trigger.get_next_fire_time(previous, start)
        assert next_run.weekday() < 5 and (next_run.hour, next_run.minute) == (12, 0)
        previous = next_run
        start = next_run + timedelta(seconds=1)
    # A fresh scheduler after noon goes to the next weekday, with no retroactive job.
    assert job.trigger.get_next_fire_time(None, datetime(2026, 9, 11, 12, 1, tzinfo=MOSCOW_TZ)) == datetime(2026, 9, 14, 12, tzinfo=MOSCOW_TZ)


async def test_manual_and_automatic_same_greeting_and_independent_dispatch_ledger(database, monkeypatch):
    freeze_day(monkeypatch, TODAY)
    kwargs = await setup_dispatch(database)
    group, _ = await kwargs["digest_service"].build("p")
    personal, _ = await kwargs["personal_service"].build(UID)
    async with database[1]() as session:
        assert await session.scalar(select(func.count()).select_from(DailyDispatch)) == 0
    await run_daily_dispatch(**kwargs)
    await run_daily_dispatch(**kwargs)
    assert kwargs["bot"].send_message.await_count == 2
    greetings = [group[0].splitlines()[0], personal[0].splitlines()[0]] + [
        call.kwargs["text"].splitlines()[0] for call in kwargs["bot"].send_message.await_args_list
    ]
    assert len(set(greetings)) == 1


async def test_personal_failure_does_not_stop_others_or_record_success(database, monkeypatch):
    freeze_day(monkeypatch, TODAY)
    kwargs = await setup_dispatch(database)
    other_uid = "22222222-2222-2222-2222-222222222222"
    await PersonalIdentity(database[1], {other_uid: "@another_user"}).resolve(43, "another_user", action="start")
    delivered = []

    async def send(*, chat_id, text):
        if chat_id == 42:
            raise RuntimeError("recipient unavailable")
        delivered.append(chat_id)

    kwargs["bot"].send_message.side_effect = send
    await run_daily_dispatch(**kwargs)
    assert delivered == [-1001, 43]
    async with database[1]() as session:
        assert await session.get(DailyDispatch, (42, TODAY)) is None
        assert await session.get(DailyDispatch, (43, TODAY)) is not None


async def test_partial_personal_send_not_success_disabled_and_changed_bindings_skipped(database):
    identity = PersonalIdentity(database[1], {UID: "@some_user"})
    await identity.resolve(42, "some_user", action="start")
    sender = AsyncMock(side_effect=RuntimeError("second chunk failed"))
    kwargs = dict(telegram_id=42, expected_user_id=UID, dispatch_date=TODAY, sender=sender)
    with pytest.raises(RuntimeError):
        await dispatch_personal_once(database[1], **kwargs)
    async with database[1]() as session:
        assert await session.get(DailyDispatch, (42, TODAY)) is None
    await identity.resolve(42, "some_user", action="stop")
    assert not await dispatch_personal_once(database[1], **kwargs)
    await identity.resolve(42, "some_user", action="start")
    assert not await dispatch_personal_once(database[1], **(kwargs | {"expected_user_id": "other"}))
    sender.side_effect = None
    assert await dispatch_personal_once(database[1], **kwargs)
    assert not await dispatch_personal_once(database[1], **kwargs)


async def test_snapshot_failure_sends_no_empty_digests(database, monkeypatch):
    freeze_day(monkeypatch, TODAY)
    kwargs = await setup_dispatch(database)
    kwargs["yougile"].fetch_workspace.side_effect = YouGileAPIError("unavailable")
    await run_daily_dispatch(**kwargs)
    kwargs["bot"].send_message.assert_not_awaited()


@pytest.mark.parametrize("day", [12, 13])
async def test_manual_group_and_personal_commands_work_on_weekends(database, monkeypatch, day):
    from aiogram.enums import ChatType
    from app.telegram_handlers import create_router
    from test_telegram_handlers import RecordingActionContext

    freeze_day(monkeypatch, date(2026, 9, day))
    kwargs = await setup_dispatch(database)
    monkeypatch.setattr("app.telegram_handlers.ChatActionSender.typing", lambda **_: RecordingActionContext([]))
    router = create_router(session_factory=database[1], yougile=kwargs["yougile"],
                           digest_service=kwargs["digest_service"], personal_service=kwargs["personal_service"],
                           personal_identity=PersonalIdentity(database[1], {}))
    for chat_id, chat_type in [(-1001, ChatType.SUPERGROUP), (42, ChatType.PRIVATE)]:
        message = SimpleNamespace(chat=SimpleNamespace(id=chat_id, type=chat_type),
                                  from_user=SimpleNamespace(id=42, username="changed_user"),
                                  message_thread_id=None, answer=AsyncMock())
        await command_handler(router, "task")(message, kwargs["bot"])
        message.answer.assert_awaited_once()
        assert message.answer.await_args.args[0].startswith("☀️ ")
    async with database[1]() as session:
        assert await session.scalar(select(func.count()).select_from(DailyDispatch)) == 0
