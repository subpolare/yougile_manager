from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from app.yougile import WorkspaceSnapshot, YouGileProject, YouGileTask, YouGileUser


MOSCOW_TZ = ZoneInfo("Europe/Moscow")
PROJECT_NUMBER_RE = re.compile(r"^#(\d+)\b")
EXCLUDED_PROJECT_PREFIX = "#5 Голова"


@dataclass(frozen=True, slots=True)
class TaskBuckets:
    today: tuple[YouGileTask, ...]
    week: tuple[YouGileTask, ...]
    overdue: tuple[YouGileTask, ...]


def project_number(project: YouGileProject) -> int | None:
    match = PROJECT_NUMBER_RE.match(project.title)
    return int(match.group(1)) if match else None


def is_eligible_project(project: YouGileProject) -> bool:
    return (
        not project.deleted
        and PROJECT_NUMBER_RE.match(project.title) is not None
        and not project.title.startswith(EXCLUDED_PROJECT_PREFIX)
    )


def match_projects(projects: list[YouGileProject], argument: str) -> list[YouGileProject]:
    argument = argument.strip()
    eligible = [project for project in projects if is_eligible_project(project)]
    if argument.isdecimal():
        pattern = re.compile(rf"^#{re.escape(argument)}\b")
        return [project for project in eligible if pattern.match(project.title)]
    return [project for project in eligible if project.title == argument]


def deadline_datetime(task: YouGileTask) -> datetime:
    if task.deadline_ms is None:
        raise ValueError("Task has no deadline")
    return datetime.fromtimestamp(task.deadline_ms / 1000, tz=UTC).astimezone(MOSCOW_TZ)


def bucket_tasks(tasks: tuple[YouGileTask, ...] | list[YouGileTask], today: date) -> TaskBuckets:
    upcoming_monday = today + timedelta(days=7 - today.weekday())
    today_items: list[YouGileTask] = []
    week_items: list[YouGileTask] = []
    overdue_items: list[YouGileTask] = []

    for task in tasks:
        if task.completed or task.archived or task.deleted or task.deadline_ms is None:
            continue
        due_date = deadline_datetime(task).date()
        if due_date == today:
            today_items.append(task)
        elif today < due_date < upcoming_monday:
            week_items.append(task)
        elif due_date < today:
            overdue_items.append(task)

    def key(task: YouGileTask) -> tuple[int, int, datetime, str]:
        # Tasks produced by WorkspaceSnapshot always have these positions. The
        # sentinels keep the pure bucketing helper usable for isolated raw tasks.
        board_order = task.board_order if task.board_order is not None else 2**31
        column_order = task.column_order if task.column_order is not None else 2**31
        return board_order, column_order, deadline_datetime(task), task.title.casefold()

    return TaskBuckets(
        today=tuple(sorted(today_items, key=key)),
        week=tuple(sorted(week_items, key=key)),
        overdue=tuple(sorted(overdue_items, key=key)),
    )


def user_names(users: tuple[YouGileUser, ...] | list[YouGileUser]) -> dict[str, str]:
    return {user.id: user.real_name for user in users}


def project_buckets(
    snapshot: WorkspaceSnapshot, project_id: str, *, today: date | None = None
) -> TaskBuckets:
    moscow_today = today or datetime.now(MOSCOW_TZ).date()
    return bucket_tasks(snapshot.tasks_for_project(project_id), moscow_today)


class DigestService:
    def __init__(self, yougile_client: object, telegram_by_user_id: dict[str, str]) -> None:
        self._yougile_client = yougile_client
        self._telegram_by_user_id = telegram_by_user_id

    async def build(
        self,
        project_id: str,
        *,
        snapshot: WorkspaceSnapshot | None = None,
        today: date | None = None,
    ) -> tuple[list[str], TaskBuckets]:
        from app.formatter import format_digest

        if snapshot is None:
            snapshot = await self._yougile_client.fetch_workspace()  # type: ignore[attr-defined]
        moscow_today = today or datetime.now(MOSCOW_TZ).date()
        buckets = project_buckets(snapshot, project_id, today=moscow_today)
        messages = format_digest(
            buckets,
            self._telegram_by_user_id,
            user_names(snapshot.users),
            today=moscow_today,
        )
        return messages, buckets
