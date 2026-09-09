from __future__ import annotations

from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

from app.task_service import bucket_tasks, deadline_datetime, project_buckets
from app.yougile import (
    WorkspaceSnapshot,
    YouGileBoard,
    YouGileColumn,
    YouGileTask,
    _parse_task,
)


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
    board_order: int | None = None,
    column_order: int | None = None,
) -> YouGileTask:
    return YouGileTask(
        id=task_id,
        title=title or task_id,
        column_id="column",
        deadline_ms=deadline_ms,
        completed=completed,
        archived=archived,
        deleted=deleted,
        board_order=board_order,
        column_order=column_order,
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


def test_tasks_use_column_position_instead_of_column_title_order() -> None:
    tasks = [
        task(
            "alphabetically-first",
            timestamp_ms(2026, 9, 7, 10),
            title="А",
            board_order=0,
            column_order=2,
        ),
        task(
            "visually-first",
            timestamp_ms(2026, 9, 7, 15),
            title="Я",
            board_order=0,
            column_order=0,
        ),
    ]
    buckets = bucket_tasks(tasks, date(2026, 9, 7))
    assert ids(buckets.today) == ["visually-first", "alphabetically-first"]


def test_snapshot_column_sequence_drives_order_and_enriches_task_titles() -> None:
    snapshot = WorkspaceSnapshot(
        boards=(
            YouGileBoard(
                id="board",
                project_id="project",
                title="Доска",
                display_order=0,
            ),
        ),
        columns=(
            YouGileColumn(
                id="visually-first-column",
                board_id="board",
                title="Я-первая",
                display_order=0,
            ),
            YouGileColumn(
                id="alphabetically-first-column",
                board_id="board",
                title="А-вторая",
                display_order=1,
            ),
        ),
        tasks=(
            YouGileTask(
                id="alphabetically-first",
                title="Задача A",
                column_id="alphabetically-first-column",
                deadline_ms=timestamp_ms(2026, 9, 7, 9),
            ),
            YouGileTask(
                id="visually-first",
                title="Задача Я",
                column_id="visually-first-column",
                deadline_ms=timestamp_ms(2026, 9, 7, 18),
            ),
        ),
        users=(),
    )

    buckets = project_buckets(snapshot, "project", today=date(2026, 9, 7))

    assert ids(buckets.today) == ["visually-first", "alphabetically-first"]
    assert buckets.today[0].column_id == "visually-first-column"
    assert buckets.today[0].column_title == "Я-первая"
    assert buckets.today[0].column_order == 0


def test_board_position_precedes_column_position() -> None:
    tasks = [
        task(
            "second-board",
            timestamp_ms(2026, 9, 7, 9),
            board_order=1,
            column_order=0,
        ),
        task(
            "first-board",
            timestamp_ms(2026, 9, 7, 18),
            board_order=0,
            column_order=10,
        ),
    ]
    buckets = bucket_tasks(tasks, date(2026, 9, 7))
    assert ids(buckets.today) == ["first-board", "second-board"]


def test_deadline_and_title_sort_tasks_inside_the_same_column() -> None:
    tasks = [
        task(
            "later",
            timestamp_ms(2026, 9, 7, 18),
            board_order=0,
            column_order=0,
        ),
        task(
            "same-z",
            timestamp_ms(2026, 9, 7, 10),
            title="Я",
            board_order=0,
            column_order=0,
        ),
        task(
            "same-a",
            timestamp_ms(2026, 9, 7, 10),
            title="А",
            board_order=0,
            column_order=0,
        ),
    ]
    buckets = bucket_tasks(tasks, date(2026, 9, 7))
    assert ids(buckets.today) == ["same-a", "same-z", "later"]


def test_overdue_tasks_are_sorted_oldest_to_newest_inside_a_column() -> None:
    tasks = [
        task(
            "recent",
            timestamp_ms(2026, 9, 6),
            board_order=0,
            column_order=0,
        ),
        task(
            "oldest",
            timestamp_ms(2026, 8, 20),
            board_order=0,
            column_order=0,
        ),
        task(
            "middle",
            timestamp_ms(2026, 9, 1),
            board_order=0,
            column_order=0,
        ),
    ]
    buckets = bucket_tasks(tasks, date(2026, 9, 7))
    assert ids(buckets.overdue) == ["oldest", "middle", "recent"]
