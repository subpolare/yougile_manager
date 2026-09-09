from __future__ import annotations

import html
import random
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from app.greetings import DUMB_GREETINGS
from app.task_service import MOSCOW_TZ, TaskBuckets, deadline_datetime
from app.yougile import YouGileTask


TELEGRAM_MESSAGE_LIMIT = 4096
NO_TASKS = "На сегодня и до конца недели задач с дедлайнами нет 🎉"
NO_ASSIGNEES = "вы забыли написать, кто за это отвечает"
_B_TAG_RE = re.compile(r"</?b>")
RUSSIAN_WEEKDAYS = (
    "понедельник",
    "вторник",
    "среда",
    "четверг",
    "пятница",
    "суббота",
    "воскресенье",
)
RUSSIAN_MONTHS = (
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
)


@dataclass(frozen=True, slots=True)
class _TaskLine:
    number: int
    task: YouGileTask
    assignees: str | None
    deadline_annotation: str | None = None

    def subject(
        self,
        title: str | None = None,
        parent_title: str | None = None,
    ) -> str:
        if not self.task.column_title:
            raise ValueError(f"Task {self.task.id!r} has no YouGile column title")
        rendered_title = self.task.title if title is None else title
        subject = f"{self.task.column_title}. {rendered_title}"

        if self.task.parent_task_id is not None:
            if self.task.parent_task_title is None:
                raise ValueError(f"Subtask {self.task.id!r} has no parent task title")
            rendered_parent = (
                self.task.parent_task_title if parent_title is None else parent_title
            )
            subject += f" (подзадача внутри «{rendered_parent}»)"
            if self.deadline_annotation:
                subject += f", {self.deadline_annotation}"
        elif self.deadline_annotation:
            subject = f"{subject} {self.deadline_annotation}"
        return subject

    def plain(
        self,
        title: str | None = None,
        assignees: str | None = None,
        parent_title: str | None = None,
    ) -> str:
        subject = self.subject(title, parent_title)
        if self.assignees is None:
            return f"{self.number}. {subject} — {NO_ASSIGNEES}"
        rendered_assignees = self.assignees if assignees is None else assignees
        return f"{self.number}. {subject}: {rendered_assignees}"

    def render(self, max_visible_units: int = TELEGRAM_MESSAGE_LIMIT) -> str:
        original = self.plain()
        if _utf16_length(original) <= max_visible_units:
            return html.escape(original)

        # Prefer shortening only one user-controlled title at a time.
        best = _best_truncated(
            self.task.title,
            lambda shortened: self.plain(title=shortened),
            max_visible_units,
        )
        if best is not None:
            return html.escape(best)

        if self.task.parent_task_id is not None and self.task.parent_task_title is not None:
            best = _best_truncated(
                self.task.parent_task_title,
                lambda shortened: self.plain(parent_title=shortened),
                max_visible_units,
            )
            if best is not None:
                return html.escape(best)

            # If both titles are pathological, keep the relationship structure and
            # allocate the remaining room to the subtask title.
            best = _best_truncated(
                self.task.title,
                lambda shortened: self.plain(title=shortened, parent_title="…"),
                max_visible_units,
            )
            if best is not None:
                return html.escape(best)

        # Extremely large assignee data is also bounded so Telegram never rejects the request.
        minimal_parent = "…" if self.task.parent_task_id is not None else None
        prefix = f"{self.number}. {self.subject(title='…', parent_title=minimal_parent)}"
        if self.assignees is None:
            minimal = self.plain(title="…", parent_title=minimal_parent)
            return html.escape(_truncate_utf16(minimal, max_visible_units))
        separator = ": "
        available = max(0, max_visible_units - _utf16_length(prefix + separator))
        shortened_assignees = _truncate_utf16(self.assignees, available)
        return html.escape(prefix + separator + shortened_assignees)


@dataclass(frozen=True, slots=True)
class _Section:
    heading: str
    lines: tuple[_TaskLine, ...]

    def render(self) -> str:
        heading = f"<b>{html.escape(self.heading)}</b>"
        items = "\n".join(line.render() for line in self.lines)
        return f"{heading}\n\n{items}"


def format_task_count(count: int) -> str:
    """Inflect 'задача' as a direct object for any non-negative count."""
    if isinstance(count, bool) or not isinstance(count, int):
        raise TypeError("count must be an integer")
    if count < 0:
        raise ValueError("count must be non-negative")
    last_two = count % 100
    last = count % 10
    if 11 <= last_two <= 14:
        word = "задач"
    elif last == 1:
        word = "задачу"
    elif 2 <= last <= 4:
        word = "задачи"
    else:
        word = "задач"
    return f"{count} {word}"


def russian_weekday(value: date) -> str:
    return RUSSIAN_WEEKDAYS[value.weekday()]


def russian_month(month: int) -> str:
    if isinstance(month, bool) or not isinstance(month, int):
        raise TypeError("month must be an integer")
    if not 1 <= month <= 12:
        raise ValueError("month must be between 1 and 12")
    return RUSSIAN_MONTHS[month - 1]


def format_future_deadline(deadline: date, today: date) -> str:
    formatted = deadline.strftime("%d.%m")
    if deadline == today + timedelta(days=1):
        return f"(до завтра, {formatted})"
    if deadline == today + timedelta(days=2):
        return f"(до послезавтра, {formatted})"
    return f"(до {formatted})"


def format_overdue_deadline(deadline: date, today: date) -> str:
    formatted = deadline.strftime("%d.%m")
    if deadline == today - timedelta(days=1):
        return f"(дедлайн вчера, {formatted})"
    if deadline == today - timedelta(days=2):
        return f"(дедлайн позавчера, {formatted})"
    return f"(дедлайн {formatted})"


def format_subtask_future_deadline(deadline: date, today: date) -> str:
    formatted = deadline.strftime("%d.%m")
    if deadline == today + timedelta(days=1):
        return f"до завтра, {formatted}"
    if deadline == today + timedelta(days=2):
        return f"до послезавтра, {formatted}"
    return f"до {formatted}"


def format_subtask_overdue_deadline(deadline: date, today: date) -> str:
    formatted = deadline.strftime("%d.%m.%Y")
    if deadline == today - timedelta(days=1):
        return f"дедлайн вчера, {formatted}"
    if deadline == today - timedelta(days=2):
        return f"дедлайн позавчера, {formatted}"
    return f"дедлайн {formatted}"


def format_assignees(
    assignee_ids: tuple[str, ...] | list[str],
    telegram_by_user_id: dict[str, str],
    display_name_by_user_id: dict[str, str],
) -> str | None:
    if not assignee_ids:
        return None
    tags: list[str] = []
    names: list[str] = []
    seen: set[str] = set()
    for user_id in assignee_ids:
        if user_id in seen:
            continue
        seen.add(user_id)
        telegram = telegram_by_user_id.get(user_id)
        if telegram:
            tags.append(telegram)
        else:
            names.append(display_name_by_user_id.get(user_id, "Неизвестный пользователь"))
    if tags and names:
        return f"{', '.join(tags)} и {', '.join(names)}"
    return ", ".join(tags or names)


def format_digest(
    buckets: TaskBuckets,
    telegram_by_user_id: dict[str, str],
    display_name_by_user_id: dict[str, str],
    *,
    today: date | None = None,
    max_length: int = TELEGRAM_MESSAGE_LIMIT,
) -> list[str]:
    moscow_today = today or datetime.now(MOSCOW_TZ).date()
    intro = f"☀️ {html.escape(random.choice(DUMB_GREETINGS))}, коллеги!"
    sections = _build_sections(
        buckets,
        telegram_by_user_id,
        display_name_by_user_id,
        moscow_today,
    )
    if not sections:
        return [f"{intro}\n\n{NO_TASKS}"]

    full = "\n\n".join([intro, *(section.render() for section in sections)])
    if telegram_visible_length(full) <= max_length:
        return [full]

    chunks: list[str] = []
    current = intro
    for section in sections:
        rendered = section.render()
        candidate = f"{current}\n\n{rendered}" if current else rendered
        if telegram_visible_length(candidate) <= max_length:
            current = candidate
            continue

        if current:
            chunks.append(current)
            current = ""
        if telegram_visible_length(rendered) <= max_length:
            current = rendered
            continue
        chunks.extend(_split_section(section, max_length))

    if current:
        chunks.append(current)
    return chunks


def telegram_visible_length(rendered_html: str) -> int:
    without_tags = _B_TAG_RE.sub("", rendered_html)
    return _utf16_length(html.unescape(without_tags))


def _build_sections(
    buckets: TaskBuckets,
    telegram_by_user_id: dict[str, str],
    display_name_by_user_id: dict[str, str],
    today: date,
) -> list[_Section]:
    specifications = (
        (
            buckets.today,
            f"Сегодня {russian_weekday(today)}, {today.day} {russian_month(today.month)}, "
            f"и вам надо закрыть {format_task_count(len(buckets.today))}:",
            None,
            None,
        ),
        (
            buckets.week,
            "Помимо этого, до конца недели есть еще "
            f"{format_task_count(len(buckets.week))}:",
            format_future_deadline,
            format_subtask_future_deadline,
        ),
        (
            buckets.overdue,
            f"А еще вы просрочили {format_task_count(len(buckets.overdue))}! "
            "Буду тегать вас, пока не исправитесь:",
            format_overdue_deadline,
            format_subtask_overdue_deadline,
        ),
    )
    sections: list[_Section] = []
    for tasks, heading, deadline_formatter, subtask_deadline_formatter in specifications:
        if not tasks:
            continue
        lines = tuple(
            _TaskLine(
                number=index,
                task=task,
                assignees=format_assignees(
                    task.assigned, telegram_by_user_id, display_name_by_user_id
                ),
                deadline_annotation=(
                    (
                        subtask_deadline_formatter
                        if task.parent_task_id is not None
                        else deadline_formatter
                    )(deadline_datetime(task).date(), today)
                    if deadline_formatter is not None
                    else None
                ),
            )
            for index, task in enumerate(tasks, start=1)
        )
        sections.append(_Section(heading=heading, lines=lines))
    return sections


def _split_section(section: _Section, max_length: int) -> list[str]:
    heading = f"<b>{html.escape(section.heading)}</b>"
    chunks: list[str] = []
    current = heading
    has_item = False

    for line in section.lines:
        separator = "\n" if has_item else "\n\n"
        rendered_line = line.render(max_length)
        candidate = current + separator + rendered_line
        if telegram_visible_length(candidate) <= max_length:
            current = candidate
            has_item = True
            continue

        if has_item:
            chunks.append(current)
            current = line.render(max_length)
            has_item = True
        else:
            # The heading is fixed and short; a pathological first line is title-truncated.
            available = max_length - telegram_visible_length(current + separator)
            current = current + separator + line.render(max(1, available))
            has_item = True

    if current:
        chunks.append(current)
    return chunks


def _utf16_length(value: str) -> int:
    return len(value.encode("utf-16-le")) // 2


def _best_truncated(
    value: str,
    render_candidate: Callable[[str], str],
    max_units: int,
) -> str | None:
    low, high = 0, len(value)
    best: str | None = None
    while low <= high:
        middle = (low + high) // 2
        shortened = value[:middle].rstrip() + "…"
        candidate = render_candidate(shortened)
        if _utf16_length(candidate) <= max_units:
            best = candidate
            low = middle + 1
        else:
            high = middle - 1
    return best


def _truncate_utf16(value: str, max_units: int) -> str:
    if _utf16_length(value) <= max_units:
        return value
    if max_units <= 0:
        return ""
    ellipsis_units = _utf16_length("…")
    if max_units < ellipsis_units:
        return ""
    result: list[str] = []
    used = 0
    for char in value:
        units = _utf16_length(char)
        if used + units + ellipsis_units > max_units:
            break
        result.append(char)
        used += units
    return "".join(result).rstrip() + "…"
