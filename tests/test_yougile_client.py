from __future__ import annotations

from urllib.parse import parse_qs

import httpx
import pytest

from app.yougile import YouGileClient, YouGileDataError, _parse_project, _parse_task


def page(content, *, offset: int = 0, limit: int = 1000, next_page: bool = False):
    return {
        "content": content,
        "paging": {"count": len(content), "limit": limit, "offset": offset, "next": next_page},
    }


async def test_every_page_is_fetched_using_documented_offset_metadata() -> None:
    offsets: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer test-key"
        query = parse_qs(request.url.query.decode())
        offsets.append(query["offset"][0])
        if query["offset"][0] == "0":
            return httpx.Response(
                200,
                json=page(
                    [
                        {"id": "one", "title": "#1 One", "timestamp": 1},
                        {"id": "two", "title": "#2 Two", "timestamp": 2},
                    ],
                    offset=0,
                    limit=2,
                    next_page=True,
                ),
            )
        return httpx.Response(
            200,
            json=page(
                [{"id": "three", "title": "#3 Three", "timestamp": 3}],
                offset=2,
                limit=2,
            ),
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://yougile.test/api-v2"
    ) as http:
        client = YouGileClient("test-key", http_client=http)
        projects = await client.fetch_projects()
    assert offsets == ["0", "2"]
    assert [project.id for project in projects] == ["one", "two", "three"]


async def test_429_retries_and_respects_zero_retry_after() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(429, headers={"Retry-After": "0"})
        return httpx.Response(200, json=page([]))

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://yougile.test/api-v2"
    ) as http:
        client = YouGileClient("test-key", http_client=http)
        assert await client.fetch_projects() == []
    assert calls == 2


async def test_workspace_uses_active_project_board_column_task_hierarchy() -> None:
    responses = {
        "/api-v2/boards": page(
            [
                {"id": "board-active", "title": "A", "projectId": "project"},
                {"id": "board-deleted", "title": "D", "projectId": "project", "deleted": True},
                {"id": "board-other", "title": "O", "projectId": "other"},
            ]
        ),
        "/api-v2/columns": page(
            [
                {"id": "column-active", "title": "A", "boardId": "board-active"},
                {"id": "column-deleted", "title": "D", "boardId": "board-active", "deleted": True},
                {"id": "column-deleted-board", "title": "DB", "boardId": "board-deleted"},
                {"id": "column-other", "title": "O", "boardId": "board-other"},
            ]
        ),
        "/api-v2/task-list": page(
            [
                {"id": "active", "title": "A", "timestamp": 1, "columnId": "column-active"},
                {"id": "deleted-column", "title": "D", "timestamp": 1, "columnId": "column-deleted"},
                {"id": "deleted-board", "title": "DB", "timestamp": 1, "columnId": "column-deleted-board"},
                {"id": "other", "title": "O", "timestamp": 1, "columnId": "column-other"},
            ]
        ),
        "/api-v2/users": page(
            [
                {
                    "id": "user",
                    "email": "u@example.test",
                    "realName": "Иван Иванов",
                    "status": "offline",
                    "lastActivity": 1,
                }
            ]
        ),
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=responses[request.url.path])

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://yougile.test/api-v2"
    ) as http:
        client = YouGileClient("test-key", http_client=http)
        snapshot = await client.fetch_workspace()
    project_tasks = snapshot.tasks_for_project("project")
    assert [task.id for task in project_tasks] == ["active"]
    assert project_tasks[0].column_id == "column-active"
    assert project_tasks[0].column_title == "A"
    assert project_tasks[0].board_order == 0
    assert project_tasks[0].column_order == 0
    assert snapshot.users[0].real_name == "Иван Иванов"


async def test_documented_subtask_ids_resolve_from_task_list_without_duplicates() -> None:
    responses = {
        "/api-v2/boards": page(
            [{"id": "board", "title": "Board", "projectId": "project"}]
        ),
        "/api-v2/columns": page(
            [{"id": "column", "title": "Постпродакшн", "boardId": "board"}]
        ),
        "/api-v2/task-list": page(
            [
                {
                    "id": "parent",
                    "title": "Родитель",
                    "timestamp": 1,
                    "columnId": "column",
                    "subtasks": ["child"],
                    "assigned": ["parent-user"],
                },
                {
                    "id": "child",
                    "title": "Подзадача",
                    "timestamp": 2,
                    "subtasks": [],
                    "assigned": ["child-user"],
                    "deadline": {
                        "deadline": 1_789_000_000_000,
                        "startDate": 1_788_000_000_000,
                    },
                },
            ]
        ),
        "/api-v2/users": page([]),
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=responses[request.url.path])

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://yougile.test/api-v2"
    ) as http:
        client = YouGileClient("test-key", http_client=http)
        snapshot = await client.fetch_workspace()

    project_tasks = snapshot.tasks_for_project("project")
    assert [task.id for task in project_tasks] == ["parent", "child"]
    assert project_tasks[0].subtask_ids == ("child",)
    assert project_tasks[1].column_id == "column"
    assert project_tasks[1].column_title == "Постпродакшн"
    assert project_tasks[1].parent_task_id == "parent"
    assert project_tasks[1].parent_task_title == "Родитель"
    assert project_tasks[1].assigned == ("child-user",)
    assert project_tasks[1].deadline_ms == 1_789_000_000_000


async def test_malformed_pagination_is_not_treated_as_empty() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"content": []})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://yougile.test/api-v2"
    ) as http:
        client = YouGileClient("test-key", http_client=http)
        with pytest.raises(YouGileDataError):
            await client.fetch_projects()


def test_nullable_optional_fields_match_live_api_representation() -> None:
    project = _parse_project(
        {"id": "project", "title": "#1 Project", "timestamp": 1, "deleted": None}
    )
    task = _parse_task(
        {
            "id": "task",
            "title": "Task",
            "timestamp": 1,
            "columnId": "column",
            "deleted": None,
            "assigned": None,
        }
    )
    assert project.deleted is False
    assert task.deleted is False
    assert task.assigned == ()


def test_subtask_ids_are_validated_and_deduplicated_stably() -> None:
    parsed = _parse_task(
        {
            "id": "task",
            "title": "Task",
            "timestamp": 1,
            "subtasks": ["child-1", "child-2", "child-1"],
        }
    )
    assert parsed.subtask_ids == ("child-1", "child-2")

    with pytest.raises(YouGileDataError, match="task.subtasks"):
        _parse_task(
            {
                "id": "bad-task",
                "title": "Bad Task",
                "timestamp": 1,
                "subtasks": [{"id": "not-documented"}],
            }
        )
