from __future__ import annotations

import re
from datetime import date, datetime
from functools import partial
from zoneinfo import ZoneInfo

import pytest

from app.formatter import (
    format_digest,
    format_future_deadline,
    format_overdue_deadline,
    format_task_count,
    russian_month,
    russian_weekday,
    telegram_visible_length,
)
from app.task_service import TaskBuckets
from app.yougile import YouGileTask


FIXED_GREETING = "С добрым утром"
format_digest_with_greeting = partial(format_digest, greeting=FIXED_GREETING)
FIXED_INTRO = f"☀️ {FIXED_GREETING}, коллеги!"
TODAY = date(2026, 9, 9)
MOSCOW = ZoneInfo("Europe/Moscow")


def task(
    task_id: str,
    title: str,
    *,
    assigned: tuple[str, ...] = ("u",),
    deadline: date = TODAY,
    column_title: str = "Редакция",
) -> YouGileTask:
    return YouGileTask(
        id=task_id,
        title=title,
        column_id="column",
        deadline_ms=int(
            datetime(
                deadline.year,
                deadline.month,
                deadline.day,
                12,
                tzinfo=MOSCOW,
            ).timestamp()
            * 1000
        ),
        assigned=assigned,
        column_title=column_title,
        board_order=0,
        column_order=0,
    )


@pytest.mark.parametrize(
    ("count", "expected"),
    [
        (0, "0 задач"),
        (1, "1 задачу"),
        (2, "2 задачи"),
        (3, "3 задачи"),
        (4, "4 задачи"),
        (5, "5 задач"),
        (10, "10 задач"),
        (11, "11 задач"),
        (12, "12 задач"),
        (13, "13 задач"),
        (14, "14 задач"),
        (20, "20 задач"),
        (21, "21 задачу"),
        (22, "22 задачи"),
        (25, "25 задач"),
        (111, "111 задач"),
        (121, "121 задачу"),
    ],
)
def test_task_count_pluralization(count: int, expected: str) -> None:
    assert format_task_count(count) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (date(2026, 9, 7), "понедельник"),
        (date(2026, 9, 8), "вторник"),
        (date(2026, 9, 9), "среда"),
        (date(2026, 9, 10), "четверг"),
        (date(2026, 9, 11), "пятница"),
        (date(2026, 9, 12), "суббота"),
        (date(2026, 9, 13), "воскресенье"),
    ],
)
def test_russian_weekday(value: date, expected: str) -> None:
    assert russian_weekday(value) == expected


def test_russian_months_use_genitive_case() -> None:
    assert [russian_month(month) for month in range(1, 13)] == [
        "января",
        "февраля",
        "марта",
        "апреля",
        "мая",
        "июня",
        "июля",
        "августа",
        "сентября",
        "октября",
        "ноября",
        "декабря",
    ]


def test_supplied_daily_greeting_is_inserted_only_once() -> None:
    today = tuple(task(str(index), "Очень длинная задача " * 4) for index in range(8))
    chunks = format_digest_with_greeting(
        TaskBuckets(today=today, week=(), overdue=()),
        {},
        {"u": "Иван"},
        today=TODAY,
        max_length=170,
    )
    assert chunks[0].startswith(FIXED_INTRO)
    assert sum(FIXED_GREETING in chunk for chunk in chunks) == 1


def test_digest_escapes_api_text_and_formats_no_assignees_without_colon() -> None:
    buckets = TaskBuckets(
        today=(task("1", "<опасная & задача>", assigned=()),),
        week=(),
        overdue=(),
    )
    result = format_digest_with_greeting(buckets, {}, {}, today=TODAY)[0]
    assert "Редакция. &lt;опасная &amp; задача&gt;" in result
    assert "&lt;опасная &amp; задача&gt;" in result
    assert "задача&gt; — вы забыли" in result
    assert "задача&gt;:" not in result


def test_exact_new_section_text_and_blank_lines() -> None:
    buckets = TaskBuckets(
        today=(task("t", "Сегодня"),),
        week=(
            task("w1", "Неделя 1", deadline=date(2026, 9, 10)),
            task("w2", "Неделя 2", deadline=date(2026, 9, 11)),
        ),
        overdue=tuple(
            task(f"o{index}", f"Долг {index}", deadline=date(2026, 9, 8))
            for index in range(5)
        ),
    )
    result = format_digest_with_greeting(buckets, {}, {"u": "Иван"}, today=TODAY)[0]
    assert result.startswith(f"{FIXED_INTRO}\n\n")
    assert (
        "<b>Сегодня среда, 9 сентября, и вам надо закрыть 1 задачу:</b>\n\n"
        "1. Редакция. Сегодня: Иван"
    ) in result
    assert (
        "<b>Помимо этого, до конца недели есть еще 2 задачи:</b>\n\n"
        "1. Редакция. Неделя 1 (до завтра, 10.09): Иван"
    ) in result
    assert (
        "<b>А еще вы просрочили 5 задач! Буду тегать вас, пока не исправитесь:</b>\n\n"
        "1. Редакция. Долг 0 (дедлайн вчера, 08.09): Иван"
    ) in result
    assert "каждое утро" not in result
    assert "\n\n\n" not in result
    assert (
        "2. Редакция. Неделя 2 (до послезавтра, 11.09): Иван\n\n"
        "<b>А еще"
    ) in result


@pytest.mark.parametrize(
    ("deadline", "expected"),
    [
        (date(2026, 9, 10), "(до завтра, 10.09)"),
        (date(2026, 9, 11), "(до послезавтра, 11.09)"),
        (date(2026, 9, 13), "(до 13.09)"),
    ],
)
def test_future_deadline_descriptions(deadline: date, expected: str) -> None:
    assert format_future_deadline(deadline, TODAY) == expected


@pytest.mark.parametrize(
    ("deadline", "expected"),
    [
        (date(2026, 9, 8), "(дедлайн вчера, 08.09)"),
        (date(2026, 9, 7), "(дедлайн позавчера, 07.09)"),
        (date(2026, 9, 3), "(дедлайн 03.09)"),
    ],
)
def test_overdue_deadline_descriptions(deadline: date, expected: str) -> None:
    assert format_overdue_deadline(deadline, TODAY) == expected


def test_today_has_no_deadline_annotation() -> None:
    buckets = TaskBuckets(today=(task("1", "Рыба выпуска"),), week=(), overdue=())
    result = format_digest_with_greeting(buckets, {}, {"u": "Иван"}, today=TODAY)[0]
    assert "1. Редакция. Рыба выпуска: Иван" in result
    assert "дедлайн" not in result


def test_no_assignee_has_column_and_overdue_deadline_without_colon() -> None:
    buckets = TaskBuckets(
        today=(),
        week=(),
        overdue=(
            task("1", "Бриф Графика", assigned=(), deadline=date(2026, 9, 8)),
        ),
    )
    result = format_digest_with_greeting(buckets, {}, {}, today=TODAY)[0]
    assert (
        "1. Редакция. Бриф Графика (дедлайн вчера, 08.09) — "
        "вы забыли написать, кто за это отвечает"
    ) in result
    assert "08.09):" not in result


def test_message_splitting_prefers_section_boundaries() -> None:
    buckets = TaskBuckets(
        today=(task("1", "Сегодня " + "длинная " * 4),),
        week=(
            task("2", "Неделя " + "длинная " * 4, deadline=date(2026, 9, 10)),
        ),
        overdue=(),
    )
    chunks = format_digest_with_greeting(buckets, {}, {"u": "Иван"}, today=TODAY, max_length=180)
    assert len(chunks) >= 2
    assert chunks[0].startswith(f"{FIXED_INTRO}\n\n")
    assert sum(FIXED_INTRO in chunk for chunk in chunks) == 1
    assert all(telegram_visible_length(chunk) <= 180 for chunk in chunks)
    assert chunks[1].startswith("<b>Помимо этого")
    assert all("</b>\n\n1. " in chunk for chunk in chunks)


def test_oversized_section_splits_only_between_items_and_keeps_numbering() -> None:
    today = tuple(task(str(index), f"Задача {index} " + "длинная " * 5) for index in range(1, 9))
    buckets = TaskBuckets(today=today, week=(), overdue=())
    chunks = format_digest_with_greeting(buckets, {}, {"u": "Иван"}, today=TODAY, max_length=180)
    assert len(chunks) > 2
    assert chunks[0] == FIXED_INTRO
    assert chunks[1].startswith("<b>Сегодня среда, 9 сентября")
    assert "</b>\n\n1. " in chunks[1]
    assert all(telegram_visible_length(chunk) <= 180 for chunk in chunks)
    numbering = [
        int(match)
        for chunk in chunks
        for match in re.findall(r"(?m)^(\d+)\. ", chunk)
    ]
    assert numbering == list(range(1, 9))
    assert all("…" not in chunk for chunk in chunks)


def test_pathological_task_title_is_truncated_safely() -> None:
    buckets = TaskBuckets(today=(task("1", "<&>" * 300),), week=(), overdue=())
    chunks = format_digest_with_greeting(buckets, {}, {"u": "Иван"}, today=TODAY, max_length=210)
    assert all(telegram_visible_length(chunk) <= 210 for chunk in chunks)
    assert "…" in "".join(chunks)
    assert "<" not in "".join(chunks).replace("<b>", "").replace("</b>", "")
