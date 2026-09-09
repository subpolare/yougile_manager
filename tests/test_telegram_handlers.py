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

    async def is_admin(*_: object) -> bool:
        events.append("admin-validation")
        return True

    async def fetch_projects() -> list[YouGileProject]:
        events.append("yougile")
        return [YouGileProject(id="project", title="#7 Project")]

    async def replace_binding(*_: object, **__: object) -> None:
        events.append("database")

    def typing(**_: object) -> RecordingActionContext:
        events.append("typing-created")
        return RecordingActionContext(events)

    monkeypatch.setattr("app.telegram_handlers._is_admin", is_admin)
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
        "admin-validation",
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
