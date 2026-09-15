from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

from aiogram.enums import ChatType

from app.task_service import TaskBuckets
from app.telegram_handlers import create_router
from app.yougile import YouGileProject


class RecordingActionContext:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    async def __aenter__(self) -> RecordingActionContext:
        self.events.append("typing-enter")
        return self

    async def __aexit__(self, *_: object) -> None:
        self.events.append("typing-exit")


def command_handler(router, name: str):
    return next(
        handler.callback
        for handler in router.message.handlers
        if handler.callback.__name__ == name
    )


def fake_message(events: list[str]):
    async def answer(_: str) -> None:
        events.append("answer")

    return SimpleNamespace(
        chat=SimpleNamespace(id=-1001, type=ChatType.SUPERGROUP),
        from_user=SimpleNamespace(id=42),
        message_thread_id=7,
        answer=AsyncMock(side_effect=answer),
    )


async def test_task_typing_covers_database_fetch_processing_and_response(monkeypatch) -> None:
    events: list[str] = []
    message = fake_message(events)
    binding = SimpleNamespace(yougile_project_id="project")

    async def get_binding(*_: object) -> object:
        events.append("database")
        return binding

    async def build(*_: object) -> tuple[list[str], TaskBuckets]:
        events.append("yougile-processing")
        return ["digest"], TaskBuckets(today=(), week=(), overdue=())

    def typing(**kwargs: object) -> RecordingActionContext:
        assert kwargs["chat_id"] == -1001
        assert kwargs["message_thread_id"] == 7
        events.append("typing-created")
        return RecordingActionContext(events)

    monkeypatch.setattr("app.telegram_handlers.get_binding", get_binding)
    monkeypatch.setattr("app.telegram_handlers.ChatActionSender.typing", typing)
    digest_service = SimpleNamespace(build=build)
    router = create_router(
        session_factory=object(),
        yougile=SimpleNamespace(),
        digest_service=digest_service,
    )

    await command_handler(router, "task")(message, SimpleNamespace())

    assert events == [
        "typing-created",
        "typing-enter",
        "database",
        "yougile-processing",
        "answer",
        "typing-exit",
    ]


async def test_init_typing_starts_after_validation_and_covers_external_work(monkeypatch) -> None:
    events: list[str] = []
    message = fake_message(events)

    async def fetch_projects() -> list[YouGileProject]:
        events.append("yougile")
        return [YouGileProject(id="project", title="#7 Project")]

    async def replace_binding(*_: object, **__: object) -> None:
        events.append("database")

    def typing(**_: object) -> RecordingActionContext:
        events.append("typing-created")
        return RecordingActionContext(events)

    monkeypatch.setattr("app.telegram_handlers.replace_binding", replace_binding)
    monkeypatch.setattr("app.telegram_handlers.ChatActionSender.typing", typing)
    router = create_router(
        session_factory=object(),
        yougile=SimpleNamespace(fetch_projects=fetch_projects),
        digest_service=SimpleNamespace(),
    )

    await command_handler(router, "initialize")(
        message,
        SimpleNamespace(args="7"),
        SimpleNamespace(),
    )

    assert events == [
        "typing-created",
        "typing-enter",
        "yougile",
        "database",
        "answer",
        "typing-exit",
    ]


async def test_status_does_not_start_typing(monkeypatch) -> None:
    events: list[str] = []
    message = fake_message(events)
    binding = SimpleNamespace(project_title="#7 Project", yougile_project_id="project")
    get_binding = AsyncMock(return_value=binding)

    def forbidden_typing(**_: object) -> RecordingActionContext:
        raise AssertionError("/status must not start a chat action")

    monkeypatch.setattr("app.telegram_handlers.get_binding", get_binding)
    monkeypatch.setattr("app.telegram_handlers.ChatActionSender.typing", forbidden_typing)
    router = create_router(
        session_factory=object(),
        yougile=SimpleNamespace(),
        digest_service=SimpleNamespace(),
    )

    await command_handler(router, "status")(message)

    get_binding.assert_awaited_once()
    assert events == ["answer"]


import pytest
from app.db import get_binding, replace_binding as save_binding


@pytest.mark.parametrize('member_status', ['member', 'administrator', 'creator'])
@pytest.mark.parametrize('chat_type', [ChatType.GROUP, ChatType.SUPERGROUP])
@pytest.mark.parametrize('argument', ['7', '#7 Project'])
async def test_init_any_member_moves_existing_binding_without_admin_lookup(
        database, monkeypatch, member_status, chat_type, argument):
    factory = database[1]
    await save_binding(factory, chat_id=-1002, project_id='p7', project_number=7, project_title='#7 Project')
    await save_binding(factory, chat_id=-1001, project_id='old', project_number=8, project_title='#8 Old')
    msg = fake_message([])
    msg.chat.type = chat_type
    bot = AsyncMock()
    bot.get_chat_member.return_value = SimpleNamespace(status=member_status)
    monkeypatch.setattr('app.telegram_handlers.ChatActionSender.typing', lambda **_: RecordingActionContext([]))
    router = create_router(session_factory=factory, yougile=SimpleNamespace(fetch_projects=AsyncMock(
        return_value=[YouGileProject('p7', '#7 Project'), YouGileProject('p70', '#70 Other')])),
        digest_service=SimpleNamespace())
    await command_handler(router, 'initialize')(msg, SimpleNamespace(args=argument), bot)
    bot.get_chat_member.assert_not_awaited()
    bot.get_chat_administrators.assert_not_awaited()
    assert await get_binding(factory, -1002) is None
    row = await get_binding(factory, -1001)
    assert (row.yougile_project_id, row.project_number, row.project_title) == ('p7', 7, '#7 Project')
    msg.answer.assert_awaited_once_with('Готово! Связал этот чат (ID: -1001) с проектом #7 Project в YouGile. '
                                        'Теперь буду спамить вам уведомлениями о задачах, вам (не) понравится')


@pytest.mark.parametrize('argument,titles', [
    ('', ['#7 Project']), ('7', ['#7 First', '#7 Second']),
    ('7', ['#70 Other', '#7suffix', 'Project #7']),
    ('#7 Proj', ['#7 Project']), ('5', ['#5 Голова компании']),
    ('#7 Same', ['#7 Same', '#7 Same']), ('7foo', ['#7 Project']),
])
async def test_invalid_init_by_member_preserves_existing_binding(database, monkeypatch, argument, titles):
    factory = database[1]
    await save_binding(factory, chat_id=-1001, project_id='old', project_number=8, project_title='#8 Old')
    bot = AsyncMock()
    monkeypatch.setattr('app.telegram_handlers.ChatActionSender.typing', lambda **_: RecordingActionContext([]))
    router = create_router(session_factory=factory, yougile=SimpleNamespace(fetch_projects=AsyncMock(
        return_value=[YouGileProject(str(i), title) for i, title in enumerate(titles)])), digest_service=SimpleNamespace())
    await command_handler(router, 'initialize')(fake_message([]), SimpleNamespace(args=argument), bot)
    assert (await get_binding(factory, -1001)).yougile_project_id == 'old'
    bot.get_chat_member.assert_not_awaited()


async def test_init_private_still_rejected_without_api_calls(database):
    msg = fake_message([])
    msg.chat.type = ChatType.PRIVATE
    bot, yg = AsyncMock(), AsyncMock()
    router = create_router(session_factory=database[1], yougile=yg, digest_service=SimpleNamespace())
    await command_handler(router, 'initialize')(msg, SimpleNamespace(args='7'), bot)
    msg.answer.assert_awaited_once_with('Эта команда работает только в групповых чатах.')
    yg.fetch_projects.assert_not_awaited()
    bot.get_chat_member.assert_not_awaited()
    assert await get_binding(database[1], -1001) is None
