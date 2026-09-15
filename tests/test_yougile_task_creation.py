import asyncio
import json
from datetime import date, datetime
from unittest.mock import AsyncMock

import httpx
import pytest

from app.task_service import MOSCOW_TZ
from app.yougile import YouGileClient, YouGileDataError
from test_yougile_client import page
from test_voice_reminders import UID as SASHA_TEST_YG_ID


class API:
    project_title = "ONLY Саша"

    def __init__(self, existing=True):
        self.projects = [{'id': 'p', 'title': ' ' + self.project_title + ' '}]
        self.boards = [{'id': 'b', 'projectId': 'p', 'title': 'Board'}]
        self.columns = [{'id': 'c', 'boardId': 'b', 'title': ' Задачи из бота '}] if existing else []
        self.posts = []
        self.keys = {}
        self.fail_after_creation = False

    def handle(self, request):
        assert request.headers['Authorization'] == 'Bearer test-key'
        path = request.url.path.removeprefix('/api-v2')
        if request.method == 'GET':
            return httpx.Response(200, json=page(getattr(self, path[1:])))
        value = json.loads(request.content)
        self.posts.append((path, value))
        key = value['idempotencyKey']
        if key not in self.keys:
            self.keys[key] = 'c' if path == '/columns' else 'task-id'
            if path == '/columns':
                self.columns.append({'id': 'c', 'boardId': value['boardId'], 'title': value['title']})
            if self.fail_after_creation:
                self.fail_after_creation = False
                raise httpx.ReadError('Connection lost after server accepted', request=request)
        return httpx.Response(201, json={'id': self.keys[key]})


@pytest.fixture(params=["ONLY Саша", "ONLY Влад"], autouse=True)
def destination(request, monkeypatch):
    monkeypatch.setattr(API, "project_title", request.param)


def client(api):
    http = httpx.AsyncClient(transport=httpx.MockTransport(api.handle), base_url='https://yougile.test/api-v2')
    limiter = AsyncMock()
    return YouGileClient('test-key', http_client=http, rate_limiter=limiter), http, limiter


@pytest.mark.parametrize('existing', [True, False])
async def test_exact_destination_and_concurrent_column_resolution(existing):
    api = API(existing)
    api.projects += [{'id': 'deleted', 'title': api.project_title, 'deleted': True},
                     {'id': 'archived', 'title': api.project_title, 'archived': True},
                     {'id': 'wrong', 'title': 'only саша'}]
    api.boards += [{'id': 'dead', 'projectId': 'p', 'title': 'Board', 'deleted': True}]
    api.columns += [{'id': 'other', 'boardId': 'dead', 'title': 'Задачи из бота'}]
    yg, http, limiter = client(api)
    async with http:
        assert await asyncio.gather(yg.resolve_voice_task_column(api.project_title), yg.resolve_voice_task_column(api.project_title)) == ['c', 'c']
    assert len(api.posts) == (0 if existing else 1)
    if not existing:
        path, payload = api.posts[0]
        assert path == '/columns' and payload['title'] == 'Задачи из бота' and payload['boardId'] == 'b'
    assert limiter.acquire.await_count >= 6


@pytest.mark.parametrize('case', ['missing-project', 'double-project', 'no-board', 'many-boards', 'double-column'])
async def test_ambiguous_destination_fails_without_post(case):
    api = API(existing=False)
    if case == 'missing-project':
        api.projects = [{'id': 'p', 'title': 'ONLY саша'}]
    if case == 'double-project':
        api.projects.append({'id': 'p2', 'title': api.project_title})
    if case == 'no-board':
        api.boards = []
    if case == 'many-boards':
        api.boards.append({'id': 'b2', 'projectId': 'p', 'title': 'Board 2'})
    if case == 'double-column':
        api.columns = [{'id': 'c1', 'boardId': 'b', 'title': 'Задачи из бота'},
                       {'id': 'c2', 'boardId': 'b', 'title': 'Задачи из бота'}]
    yg, http, _ = client(api)
    async with http:
        with pytest.raises(YouGileDataError):
            await yg.resolve_voice_task_column(api.project_title)
    assert not api.posts


async def test_many_boards_with_one_existing_column_unambiguous():
    api = API()
    api.boards.append({'id': 'b2', 'projectId': 'p', 'title': 'Other'})
    yg, http, _ = client(api)
    async with http:
        assert await yg.resolve_voice_task_column(api.project_title) == 'c'
    assert not api.posts


@pytest.mark.parametrize('deadline', [date(2026, 9, 17), date(2004, 1, 19), date(2027, 1, 1)])
async def test_create_payload_no_assignee_and_moscow_calendar_day(deadline):
    api = API()
    yg, http, limiter = client(api)
    async with http:
        assert await yg.create_voice_task(project_title=api.project_title, title='Test', deadline=deadline, idempotency_key='key') == 'task-id'
    path, payload = api.posts[0]
    assert path == '/tasks'
    assert payload['title'] == 'Test' and payload['columnId'] == 'c'
    assert payload['assigned'] == []
    assert SASHA_TEST_YG_ID not in json.dumps(payload) and 'projectId' not in payload
    value = payload['deadline']
    assert datetime.fromtimestamp(value['deadline'] / 1000, tz=MOSCOW_TZ).date() == deadline
    assert value['withTime'] is False and value['blockedPoints'] == [] and value['links'] == []
    assert limiter.acquire.await_count == 4


async def test_lost_response_retries_same_key_and_restart_reuses_column(monkeypatch):
    api = API(existing=False)
    api.fail_after_creation = True
    monkeypatch.setattr('app.yougile.asyncio.sleep', AsyncMock())
    yg, http, _ = client(api)
    async with http:
        assert await yg.resolve_voice_task_column(api.project_title) == 'c'
    assert len(api.columns) == 1
    assert api.posts[0][1]['idempotencyKey'] == api.posts[1][1]['idempotencyKey']
    restarted, http, _ = client(api)
    async with http:
        assert await restarted.resolve_voice_task_column(api.project_title) == 'c'
        api.fail_after_creation = True
        await restarted.create_voice_task(project_title=api.project_title, title='Test', deadline=date(2026, 9, 17), idempotency_key='task-key')
    task_posts = [v for p, v in api.posts if p == '/tasks']
    assert len(task_posts) == 2 and task_posts[0] == task_posts[1]
    assert len(api.keys) == 2  # one column and one task


async def test_malformed_success_is_not_accepted():
    api = API()
    original = api.handle
    def handle(request):
        if request.method == 'POST':
            return httpx.Response(201, json={})
        return original(request)
    api.handle = handle
    yg, http, _ = client(api)
    async with http:
        with pytest.raises(YouGileDataError):
            await yg.create_voice_task(project_title=api.project_title, title='Test', deadline=date(2026, 9, 17), idempotency_key='key')
