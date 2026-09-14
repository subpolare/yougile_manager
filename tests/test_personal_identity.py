from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.enums import ChatType
from sqlalchemy import func, select

from app.config import parse_employee_mappings
from app.models import PersonalDigestSubscription
from app.personal_identity import PersonalIdentity, START, STOP, RETURNING, UNKNOWN
from app.task_service import TaskBuckets
from app.telegram_handlers import create_router
from app.yougile import YouGileAPIError
from test_telegram_handlers import command_handler, RecordingActionContext


UID = "11111111-1111-1111-1111-111111111111"


@pytest.fixture
def private_setup(database, monkeypatch):
    _, factory = database
    identity = PersonalIdentity(factory, {UID: "@Some_User"})
    events = []
    monkeypatch.setattr("app.telegram_handlers.ChatActionSender.typing",
                        lambda **kwargs: RecordingActionContext(events))
    message = SimpleNamespace(chat=SimpleNamespace(id=42, type=ChatType.PRIVATE),
                              from_user=SimpleNamespace(id=42, username="some_user"),
                              answer=AsyncMock(), message_thread_id=None)
    personal = SimpleNamespace(build=AsyncMock(return_value=(["digest"], TaskBuckets((), (), ()))))
    group = SimpleNamespace(build=AsyncMock(return_value=(["group"], TaskBuckets((), (), ()))))
    router = create_router(session_factory=factory, yougile=AsyncMock(), digest_service=group,
                           personal_service=personal, personal_identity=identity)
    return message, router, identity, personal, group, events


async def row(identity):
    async with identity.session_factory() as session:
        return await session.get(PersonalDigestSubscription, 42)


async def test_first_repeated_start_stop_returning_and_changed_username(private_setup):
    message, router, identity, personal, _, _ = private_setup
    await command_handler(router, "start")(message)
    message.answer.assert_awaited_with(START)
    assert (await row(identity)).enabled
    personal.build.assert_not_awaited()
    message.from_user.username = "changed_name"
    await command_handler(router, "start")(message)
    message.answer.assert_awaited_with(RETURNING)
    await command_handler(router, "stop")(message)
    message.answer.assert_awaited_with(STOP)
    saved = await row(identity)
    assert saved is not None and not saved.enabled and saved.yougile_user_id == UID
    assert saved.telegram_username == "changed_name"
    await command_handler(router, "task")(message, AsyncMock())
    personal.build.assert_awaited_with(UID)
    assert not (await row(identity)).enabled
    await command_handler(router, "start")(message)
    message.answer.assert_awaited_with(RETURNING)
    assert (await row(identity)).enabled
    message.from_user.username = None
    await command_handler(router, "task")(message, AsyncMock())
    personal.build.assert_awaited_with(UID)


async def test_task_and_stop_before_first_start_do_not_subscribe(private_setup):
    message, router, identity, personal, _, events = private_setup
    await command_handler(router, "task")(message, AsyncMock())
    personal.build.assert_awaited_with(UID)
    assert events == ["typing-enter", "typing-exit"]
    assert await row(identity) is None
    await command_handler(router, "stop")(message)
    message.answer.assert_awaited_with(STOP)
    assert await row(identity) is None
    await command_handler(router, "start")(message)
    message.answer.assert_awaited_with(START)


@pytest.mark.parametrize("command", ["start", "stop", "task"])
async def test_unknown_exact_response_no_binding(private_setup, command):
    message, router, identity, personal, _, _ = private_setup
    message.from_user.username = "not_known"
    args = (message, AsyncMock()) if command == "task" else (message,)
    await command_handler(router, command)(*args)
    message.answer.assert_awaited_once_with(UNKNOWN)
    assert await row(identity) is None
    personal.build.assert_not_awaited()


@pytest.mark.parametrize("env_name,actual", [("@Some_User", "some_USER"),
                                              ("Some_User", "@SOME_USER")])
async def test_generic_case_insensitive_optional_at(database, env_name, actual):
    identity = PersonalIdentity(database[1], parse_employee_mappings({"WHATEVER_TG": env_name, "WHATEVER_YG": UID}))
    assert await identity.resolve(42, actual, action="start") == (UID, False)


@pytest.mark.parametrize("yg,tg", [("unknown", "@some_user"), ("", "@some_user"),
                                    ("invalid", "@some_user"), (UID, "@"),
                                    (UID, "https://t.me/some_user"), (UID, "")])
async def test_invalid_whitelist_rejected(database, yg, tg):
    identity = PersonalIdentity(database[1], parse_employee_mappings({"X_TG": tg, "X_YG": yg}))
    assert await identity.resolve(42, "some_user", action="start") == (None, False)


async def test_one_to_one_and_authoritative_binding(database):
    identity = PersonalIdentity(database[1], {UID: "@some_user"})
    assert await identity.resolve(42, "some_user", action="start") == (UID, False)
    assert await identity.resolve(43, "some_user", action="start") == (None, False)
    identity.mappings = {}
    assert await identity.resolve(42, "new_user", action="start") == (UID, True)
    async with database[1]() as session:
        assert await session.scalar(select(func.count()).select_from(PersonalDigestSubscription)) == 1


async def test_group_start_does_not_subscribe(private_setup):
    message, router, identity, _, _, _ = private_setup
    message.chat.type = ChatType.SUPERGROUP
    await command_handler(router, "start")(message)
    assert await row(identity) is None


async def test_api_error_is_not_empty_digest(private_setup):
    message, router, _, personal, _, _ = private_setup
    personal.build.side_effect = YouGileAPIError("temporary")
    await command_handler(router, "task")(message, AsyncMock())
    message.answer.assert_awaited_once_with("Не удалось получить задачи из YouGile. Попробуйте /task позже.")
