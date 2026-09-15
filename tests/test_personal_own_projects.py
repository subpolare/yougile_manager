from dataclasses import replace
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest

from app.config import VOICE_TASK_PROJECTS
from app.daily_greeting import DailyGreetingProvider
from app.personal_digest import PersonalDigestService, personal_task_index, personal_project_name
from app.personal_identity import PersonalIdentity
from app.scheduler import run_daily_dispatch
from app.telegram_handlers import create_router
from app.yougile import YouGileProject
from test_personal_digest import workspace, item, TODAY
from test_personal_dispatch import freeze_day
from test_telegram_handlers import command_handler, RecordingActionContext

OWNERS = {'sasha': 'ONLY Саша', 'vlad': 'ONLY Влад', 'kostya': 'ONLY Костя'}


def own_workspace():
    titles = ('#7 Эволюция / Монтаж', *OWNERS.values(), 'Агргатные', '#23 Рак', 'ONLY Другой')
    tasks = [item('normal-' + uid, assigned=(uid,)) for uid in (*OWNERS, 'other')]
    tasks += [item('unassigned-normal', assigned=()), item('ordinary-rejected', column_id='c4', assigned=tuple(OWNERS)),
              item('excluded-normal', column_id='c5', assigned=tuple(OWNERS)),
              item('unknown-only', column_id='c6', assigned=(*OWNERS, 'other'))]
    for i, uid in enumerate(OWNERS, 1):
        tasks += [item(uid + '-unassigned', column_id=f'c{i}', assigned=()),
                  item(uid + '-assigned-others', column_id=f'c{i}', assigned=(*OWNERS, 'other')),
                  item(uid + '-parent', column_id=f'c{i}', assigned=(), subtask_ids=(uid + '-child', uid + '-no-date-child')),
                  item(uid + '-child', column_id=None, assigned=()),
                  item(uid + '-no-date-child', column_id=None, assigned=(), deadline_ms=None)]
        for name, state in [('done', {'completed': True}), ('archived', {'archived': True}),
                            ('deleted', {'deleted': True}), ('no-date', {'deadline_ms': None})]:
            tasks.append(item(uid + '-' + name, column_id=f'c{i}', assigned=(), **state))
    return workspace(tasks, titles=titles)


@pytest.mark.parametrize('uid', [*OWNERS, 'other'])
def test_only_owner_gets_all_open_dated_own_tasks_and_no_other_only(uid):
    index = personal_task_index(own_workspace(), own_projects=OWNERS)
    expected = {'normal-' + uid}
    if uid in OWNERS:
        expected |= {uid + suffix for suffix in ('-unassigned', '-assigned-others', '-parent', '-child')}
    assert {task.id for task in index[uid]} == expected
    for task in index[uid]:
        assert task.personal_project_title in {'#7 Эволюция', OWNERS.get(uid)}
        if task.id == uid + '-child':
            assert task.parent_task_id == uid + '-parent'


@pytest.mark.parametrize('title', [*OWNERS.values(), 'ONLY Другой'])
def test_only_names_require_exact_explicit_owner(title):
    project = YouGileProject('p', title)
    assert personal_project_name(project) is None
    assert personal_project_name(project, own_project_title='ONLY чужой') is None
    assert personal_project_name(project, own_project_title=title) == title
    assert personal_project_name(replace(project, deleted=True), own_project_title=title) is None
    assert personal_project_name(replace(project, archived=True), own_project_title=title) is None
    assert personal_project_name(replace(project, title=title + ' / copy'), own_project_title=title) is None


@pytest.mark.parametrize('level', ['projects', 'boards', 'columns'])
@pytest.mark.parametrize('state', ['deleted', 'archived'])
def test_inactive_own_hierarchy_excluded(level, state):
    snapshot = own_workspace()
    entities = list(getattr(snapshot, level))
    entities[1] = replace(entities[1], **{state: True})
    snapshot = replace(snapshot, **{level: tuple(entities)})
    index = personal_task_index(snapshot, own_projects=OWNERS)
    assert {task.id for task in index['sasha']} == {'normal-sasha'}
    assert any(task.id == 'vlad-unassigned' for task in index['vlad'])


def test_missing_or_ambiguous_mapping_fails_closed():
    for owners in ({}, {'sasha': 'ONLY Саша', 'other': 'ONLY Саша'}):
        index = personal_task_index(own_workspace(), own_projects=owners)
        assert all(not task.personal_project_title.startswith('ONLY ') for tasks in index.values() for task in tasks)


@pytest.mark.parametrize('uid', [*OWNERS, 'other'])
async def test_private_task_and_scheduled_digest_share_selection(database, monkeypatch, uid):
    freeze_day(monkeypatch, TODAY)
    monkeypatch.setattr('app.telegram_handlers.ChatActionSender.typing', lambda **_: RecordingActionContext([]))
    mappings = {name: '@' + name + '_test' for name in (*OWNERS, 'other')}
    identity = PersonalIdentity(database[1], mappings, voice_task_employees={
        role: '@' + role.lower() + '_test' for role in VOICE_TASK_PROJECTS})
    assert identity.voice_task_projects == OWNERS
    await identity.resolve(42, uid + '_test', action='start')
    client = AsyncMock()
    client.fetch_workspace.return_value = own_workspace()
    personal = PersonalDigestService(client, DailyGreetingProvider(database[1]), own_projects=identity.voice_task_projects)
    router = create_router(session_factory=database[1], yougile=client, digest_service=None,
                           personal_service=personal, personal_identity=identity)
    msg = NS(chat=NS(id=42, type='private'), from_user=NS(id=42, username='changed_username'),
             message_thread_id=None, answer=AsyncMock())
    bot = AsyncMock()
    await command_handler(router, 'task')(msg, bot)
    await run_daily_dispatch(bot=bot, session_factory=database[1], yougile=client,
                             digest_service=None, personal_service=personal)
    manual = [call.args[0] for call in msg.answer.await_args_list]
    scheduled = [call.kwargs['text'] for call in bot.send_message.await_args_list]
    assert manual == scheduled
    text = '\n'.join(manual)
    assert 'normal-' + uid in text
    for owner, project in OWNERS.items():
        assert (project in text) == (uid == owner)
        assert (owner + '-unassigned' in text) == (uid == owner)
