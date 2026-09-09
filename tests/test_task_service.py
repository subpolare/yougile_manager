from __future__ import annotations

from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

from app.task_service import bucket_tasks, deadline_datetime
from app.yougile import YouGileTask, _parse_task


MOSCOW = ZoneInfo("Europe/Moscow")


def timestamp_ms(year: int, month: int, day: int, hour: int = 12) -> int:
    return int(datetime(year, month, day, hour, tzinfo=MOSCOW).timestamp() * 1000)


def task(
    task_id: str,
    deadline_ms: int | None,
    *,
    completed: bool = False,
    archived: bool = False,
    deleted: bool = False,
    title: str | None = None,
) -> YouGileTask:
    return YouGileTask(
        id=task_id,
        title=title or task_id,
        column_id="column",
        deadline_ms=deadline_ms,
        completed=completed,
        archived=archived,
        deleted=deleted,
    )


def ids(items: tuple[YouGileTask, ...]) -> list[str]:
    return [item.id for item in items]


def test_deadline_today() -> None:
    buckets = bucket_tasks([task("today", timestamp_ms(2026, 9, 7))], date(2026, 9, 7))
    assert ids(buckets.today) == ["today"]


def test_deadline_tomorrow() -> None:
    buckets = bucket_tasks([task("tomorrow", timestamp_ms(2026, 9, 8))], date(2026, 9, 7))
    assert ids(buckets.week) == ["tomorrow"]


def test_future_deadline_before_monday() -> None:
    buckets = bucket_tasks([task("sunday", timestamp_ms(2026, 9, 13))], date(2026, 9, 7))
    assert ids(buckets.week) == ["sunday"]


def test_upcoming_monday_is_excluded() -> None:
    buckets = bucket_tasks([task("monday", timestamp_ms(2026, 9, 14))], date(2026, 9, 7))
    assert not buckets.today and not buckets.week and not buckets.overdue


def test_sunday_has_no_future_week_bucket() -> None:
    buckets = bucket_tasks(
        [task("monday", timestamp_ms(2026, 9, 14))],
        date(2026, 9, 13),
    )
    assert not buckets.week


def test_overdue_task() -> None:
    buckets = bucket_tasks([task("late", timestamp_ms(2026, 9, 6))], date(2026, 9, 7))
    assert ids(buckets.overdue) == ["late"]


def test_completed_task_is_excluded() -> None:
    buckets = bucket_tasks(
        [task("done", timestamp_ms(2026, 9, 7), completed=True)], date(2026, 9, 7)
    )
    assert not buckets.today and not buckets.week and not buckets.overdue


def test_task_without_deadline_is_excluded() -> None:
    buckets = bucket_tasks([task("undated", None)], date(2026, 9, 7))
    assert not buckets.today and not buckets.week and not buckets.overdue


def test_archived_and_deleted_tasks_are_excluded() -> None:
    buckets = bucket_tasks(
        [
            task("archived", timestamp_ms(2026, 9, 7), archived=True),
            task("deleted", timestamp_ms(2026, 9, 7), deleted=True),
        ],
        date(2026, 9, 7),
    )
    assert not buckets.today


def test_buckets_do_not_overlap() -> None:
    tasks = [
        task("overdue", timestamp_ms(2026, 9, 6)),
        task("today", timestamp_ms(2026, 9, 7)),
        task("week", timestamp_ms(2026, 9, 13)),
        task("monday", timestamp_ms(2026, 9, 14)),
    ]
    buckets = bucket_tasks(tasks, date(2026, 9, 7))
    all_ids = ids(buckets.today) + ids(buckets.week) + ids(buckets.overdue)
    assert sorted(all_ids) == ["overdue", "today", "week"]
    assert len(all_ids) == len(set(all_ids))


def test_deadline_range_uses_end_deadline() -> None:
    parsed = _parse_task(
        {
            "id": "range",
            "title": "Range",
            "columnId": "column",
            "deadline": {
                "startDate": timestamp_ms(2026, 9, 7),
                "deadline": timestamp_ms(2026, 9, 8),
                "blockedPoints": [],
                "links": [],
            },
        }
    )
    buckets = bucket_tasks([parsed], date(2026, 9, 7))
    assert ids(buckets.week) == ["range"]


def test_deadline_is_converted_to_moscow_before_date_comparison() -> None:
    utc_instant = datetime(2026, 9, 8, 21, 30, tzinfo=UTC)
    moscow_task = task("timezone", int(utc_instant.timestamp() * 1000))
    assert deadline_datetime(moscow_task).date() == date(2026, 9, 9)
    buckets = bucket_tasks([moscow_task], date(2026, 9, 9))
    assert ids(buckets.today) == ["timezone"]


def test_tasks_are_sorted_by_deadline_then_title() -> None:
    tasks = [
        task("b", timestamp_ms(2026, 9, 7, 13), title="Б"),
        task("z", timestamp_ms(2026, 9, 7, 12), title="Я"),
        task("a", timestamp_ms(2026, 9, 7, 12), title="А"),
    ]
    buckets = bucket_tasks(tasks, date(2026, 9, 7))
    assert ids(buckets.today) == ["a", "z", "b"]

