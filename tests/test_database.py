from __future__ import annotations

from datetime import date

from sqlalchemy import func, select

from app.db import dispatch_once, get_binding, replace_binding
from app.models import ChatProjectBinding, DailyDispatch


async def test_replacing_existing_chat_binding(database) -> None:
    _, factory = database
    await replace_binding(
        factory, chat_id=-1001, project_id="project-a", project_number=1, project_title="#1 A"
    )
    await replace_binding(
        factory, chat_id=-1001, project_id="project-b", project_number=2, project_title="#2 B"
    )
    binding = await get_binding(factory, -1001)
    assert binding is not None
    assert binding.yougile_project_id == "project-b"
    async with factory() as session:
        assert await session.scalar(select(func.count()).select_from(ChatProjectBinding)) == 1


async def test_moving_existing_project_binding_unbinds_previous_chat(database) -> None:
    _, factory = database
    await replace_binding(
        factory, chat_id=-1001, project_id="project-a", project_number=1, project_title="#1 A"
    )
    await replace_binding(
        factory, chat_id=-1002, project_id="project-b", project_number=2, project_title="#2 B"
    )
    await replace_binding(
        factory, chat_id=-1002, project_id="project-a", project_number=1, project_title="#1 A"
    )
    assert await get_binding(factory, -1001) is None
    moved = await get_binding(factory, -1002)
    assert moved is not None and moved.yougile_project_id == "project-a"
    async with factory() as session:
        assert await session.scalar(select(func.count()).select_from(ChatProjectBinding)) == 1


async def test_daily_dispatch_is_idempotent_and_records_only_success(database) -> None:
    _, factory = database
    await replace_binding(
        factory, chat_id=-1001, project_id="project-a", project_number=1, project_title="#1 A"
    )
    calls = 0

    async def sender() -> None:
        nonlocal calls
        calls += 1

    first = await dispatch_once(
        factory,
        chat_id=-1001,
        expected_project_id="project-a",
        dispatch_date=date(2026, 9, 9),
        sender=sender,
    )
    second = await dispatch_once(
        factory,
        chat_id=-1001,
        expected_project_id="project-a",
        dispatch_date=date(2026, 9, 9),
        sender=sender,
    )
    assert first is True and second is False and calls == 1
    async with factory() as session:
        assert await session.scalar(select(func.count()).select_from(DailyDispatch)) == 1


async def test_failed_send_does_not_create_daily_dispatch(database) -> None:
    _, factory = database
    await replace_binding(
        factory, chat_id=-1001, project_id="project-a", project_number=1, project_title="#1 A"
    )

    async def sender() -> None:
        raise RuntimeError("send failed")

    try:
        await dispatch_once(
            factory,
            chat_id=-1001,
            expected_project_id="project-a",
            dispatch_date=date(2026, 9, 9),
            sender=sender,
        )
    except RuntimeError:
        pass
    else:
        raise AssertionError("sender error must propagate")
    async with factory() as session:
        assert await session.scalar(select(func.count()).select_from(DailyDispatch)) == 0

