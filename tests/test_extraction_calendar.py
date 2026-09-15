from datetime import date, datetime

import pytest

from app.openai_service import extraction_calendar, OpenAIService, YouGileTaskExtraction
from test_voice_reminders import settings, sdk_client


@pytest.mark.parametrize('phrase,expected', [
    ('сегодня', '2026-09-15'), ('завтра', '2026-09-16'), ('послезавтра', '2026-09-17'),
    ('до пятницы', '2026-09-18'), ('в пятницу', '2026-09-18'),
    ('в эту пятницу', '2026-09-18'), ('в ближайшую пятницу', '2026-09-18'),
    ('в следующую пятницу', '2026-09-25'), ('на следующей неделе в пятницу', '2026-09-25'),
    ('до понедельника', '2026-09-21'), ('в этот понедельник', '2026-09-21'),
    ('в ближайший понедельник', '2026-09-21'), ('в следующий понедельник', '2026-09-21'),
    ('в эту среду', '2026-09-16'), ('в следующую среду', '2026-09-23'),
    ('в этот вторник', '2026-09-15'), ('в следующий вторник', '2026-09-22'),
])
@pytest.mark.parametrize('kind', ['reminder', 'task'])
async def test_shared_prompt_resolves_relative_deadlines(monkeypatch, phrase, expected, kind):
    class Clock:
        @staticmethod
        def now(tz):
            assert str(tz) == 'Europe/Moscow'
            return datetime(2026, 9, 15, tzinfo=tz)
    monkeypatch.setattr('app.openai_service.datetime', Clock)
    client = sdk_client()
    ai = OpenAIService(settings(), client)
    if kind == 'task':
        client.responses.parse.return_value.output_parsed = YouGileTaskExtraction(title='Тест', deadline=None)
        await ai.extract_yougile_task('Подготовить отчёт ' + phrase)
    else:
        await ai.extract_reminder('Подготовить отчёт ' + phrase)
    options = client.responses.parse.await_args.kwargs
    anchors = dict(line.split(' = ') for line in options['instructions'].splitlines() if ' = ' in line)
    assert anchors[phrase] == expected
    assert 'НЕ возвращай deadline=null' in options['instructions']
    assert 'не придумывай его' in options['instructions']
    assert options['model'] == 'gpt-5.6-terra'
    assert options['reasoning'] == {'effort': 'none'} and options['store'] is False


@pytest.mark.parametrize('today,phrase,expected', [
    (date(2026, 9, 18), 'в пятницу', '2026-09-18'),
    (date(2026, 9, 19), 'в эту пятницу', '2026-09-25'),
    (date(2026, 9, 20), 'в следующий понедельник', '2026-09-21'),
    (date(2026, 9, 21), 'в следующий понедельник', '2026-09-28'),
    (date(2026, 12, 31), 'в следующую пятницу', '2027-01-08'),
])
def test_calendar_boundaries(today, phrase, expected):
    anchors = dict(line.split(' = ') for line in extraction_calendar(today).splitlines() if ' = ' in line)
    assert anchors[phrase] == expected
