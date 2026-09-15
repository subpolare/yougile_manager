from __future__ import annotations

import html
import re
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import date, datetime
from types import MappingProxyType

from app.daily_greeting import DailyGreetingProvider
from app.formatter import (
    TELEGRAM_MESSAGE_LIMIT, _Section, _truncate_utf16, _utf16_length,
    format_subtask_future_deadline, format_subtask_overdue_deadline,
    format_task_count, russian_month, russian_weekday, split_digest,
    telegram_visible_length,
)
from app.task_service import MOSCOW_TZ, TaskBuckets, bucket_tasks
from app.yougile import WorkspaceSnapshot, YouGileClient, YouGileProject, YouGileTask


EXCLUDED_WORDS = re.compile(r"\b(?:Голова|Шифры|Растения|Рак)\b", re.IGNORECASE)
FOOTER = "<blockquote><i>Если захочешь отменить этот дайджест, всегда можно написать мне /stop. А если хочешь получить его еще раз вне очереди — /task</i></blockquote>"
EMPTY = "На сегодня и до конца недели у тебя задач с дедлайнами нет 🎉"


def personal_project_name(
    project: YouGileProject, *, own_project_title: str | None = None,
) -> str | None:
    if project.deleted or project.archived:
        return None

    title = project.title.strip()
    # ONLY projects are private even when tasks have other assignees.
    if title.startswith("ONLY "):
        return title if title == own_project_title else None

    if not title.startswith("#"):
        return None

    title = title.split("/", 1)[0].strip()

    if title.startswith("#"):
        title = title.split("/", 1)[0].strip()
        if EXCLUDED_WORDS.search(title):
            return None
    return title


def personal_task_index(
    snapshot: WorkspaceSnapshot,
    *,
    own_projects: Mapping[str, str] | None = None,
) -> Mapping[str, tuple[YouGileTask, ...]]:
    """Index ordinary tasks by assignee and private ONLY tasks by project owner."""
    by_user: dict[str, list[YouGileTask]] = defaultdict(list)
    seen: set[str] = set()
    owners: dict[str, set[str]] = defaultdict(set)
    for uid, title in (own_projects or {}).items():
        owners[title].add(uid)

    for project in snapshot.projects:
        title = project.title.strip()
        # Ambiguous ownership fails closed instead of sharing personal content.
        candidates = owners.get(title, set())
        owner = next(iter(candidates)) if len(candidates) == 1 else None
        name = personal_project_name(project, own_project_title=title if owner else None)
        if name is None:
            continue
        for task in snapshot.tasks_for_project(project.id, exclude_archived_hierarchy=True):
            if task.id in seen:
                continue
            seen.add(task.id)
            if task.completed or task.archived or task.deleted or task.deadline_ms is None:
                continue
            item = replace(task, personal_project_title=name)
            # Project ownership includes unassigned tasks, but never adds outsiders.
            recipients = {owner} if title.startswith("ONLY ") else set(task.assigned)
            for uid in recipients:
                by_user[uid].append(item)
    return MappingProxyType({uid: tuple(items) for uid, items in by_user.items()})


def personal_buckets(tasks: tuple[YouGileTask, ...], today: date) -> TaskBuckets:
    buckets = bucket_tasks(tasks, today)

    def ordered(items: tuple[YouGileTask, ...]) -> tuple[YouGileTask, ...]:
        return tuple(sorted(items, key=lambda task: (
            task.deadline_ms, (task.personal_project_title or "").casefold(),
            task.title.casefold(), task.id,
        )))

    return TaskBuckets(ordered(buckets.today), ordered(buckets.week), ordered(buckets.overdue))


def nominative_count(count: int) -> str:
    return format_task_count(count).replace("задачу", "задача")


def overdue_heading(count: int) -> str:
    noun = nominative_count(count)
    ending = noun.split()[-1]
    verb = {"задача": "просрочена", "задачи": "просрочены", "задач": "просрочено"}[ending]
    return f"А еще у тебя {verb} {noun}:"


@dataclass(frozen=True, slots=True)
class PersonalTaskLine:
    number: int
    task: YouGileTask
    annotation: str | None

    def plain(self, project: str, title: str, parent: str | None) -> str:
        subject = f"{self.number}. {project}. {title}"
        if self.task.parent_task_id is not None:
            if parent is None:
                raise ValueError("Subtask has no immediate parent title")
            subject += f" (подзадача внутри «{parent}»)"
            if self.annotation:
                subject += f", {self.annotation}"
        elif self.annotation:
            subject += f" ({self.annotation})"
        return subject

    def render(self, max_visible_units: int = TELEGRAM_MESSAGE_LIMIT) -> str:
        project = self.task.personal_project_title
        if project is None:
            raise ValueError("Personal task has no project display name")
        parts = [project, self.task.title, self.task.parent_task_title]
        original = self.plain(*parts)
        if _utf16_length(original) <= max_visible_units:
            return html.escape(original)
        # Cap all user-controlled fields together, keeping the parent/deadline
        # structure even if all three titles contain thousands of emoji.
        low, high = 1, max_visible_units
        best = self.plain("…", "…", "…" if parts[2] is not None else None)
        while low <= high:
            cap = (low + high) // 2
            candidate = self.plain(*[
                _truncate_utf16(part, cap) if part is not None else None for part in parts
            ])
            if _utf16_length(candidate) <= max_visible_units:
                best = candidate
                low = cap + 1
            else:
                high = cap - 1
        return html.escape(_truncate_utf16(best, max_visible_units))


def format_personal_digest(
    buckets: TaskBuckets, *, today: date, greeting: str,
    max_length: int = TELEGRAM_MESSAGE_LIMIT,
) -> list[str]:
    from app.task_service import deadline_datetime

    intro = f"☀️ {html.escape(greeting)}, коллеги!"
    specs = (
        (buckets.today,
         f"Сегодня {russian_weekday(today)}, {today.day} {russian_month(today.month)}, "
         f"и лично тебе надо закрыть {format_task_count(len(buckets.today))}:", None),
        (buckets.week,
         f"Помимо этого, до конца недели есть еще {nominative_count(len(buckets.week))}:",
         format_subtask_future_deadline),
        (buckets.overdue, overdue_heading(len(buckets.overdue)), format_subtask_overdue_deadline),
    )
    sections = [
        _Section(heading, tuple(
            PersonalTaskLine(i, task, annotation(deadline_datetime(task).date(), today)
                             if annotation else None)
            for i, task in enumerate(tasks, 1)
        )) for tasks, heading, annotation in specs if tasks
    ]
    chunks = split_digest(intro, sections, max_length) if sections else [f"{intro}\n\n{EMPTY}"]
    if telegram_visible_length(chunks[-1] + "\n\n" + FOOTER) <= max_length:
        chunks[-1] += "\n\n" + FOOTER
    else:
        chunks.append(FOOTER)
    return chunks


class PersonalDigestService:
    def __init__(self, yougile_client: YouGileClient, greeting_provider: DailyGreetingProvider,
                 *, own_projects: Mapping[str, str] | None = None) -> None:
        self.yougile = yougile_client
        self.greetings = greeting_provider
        self.own_projects = dict(own_projects or {})

    def task_index(self, snapshot: WorkspaceSnapshot):
        return personal_task_index(snapshot, own_projects=self.own_projects)

    async def build(
        self, user_id: str, *, snapshot: WorkspaceSnapshot | None = None,
        today: date | None = None,
        index: Mapping[str, tuple[YouGileTask, ...]] | None = None,
    ) -> tuple[list[str], TaskBuckets]:
        if index is None:
            if snapshot is None:
                snapshot = await self.yougile.fetch_workspace()
            index = self.task_index(snapshot)
        today = today or datetime.now(MOSCOW_TZ).date()
        buckets = personal_buckets(index.get(user_id, ()), today)
        return format_personal_digest(
            buckets, today=today, greeting=await self.greetings.get(today)
        ), buckets
