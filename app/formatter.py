from __future__ import annotations

import html
import random
import re
from dataclasses import dataclass

from app.greetings import DUMB_GREETINGS
from app.task_service import TaskBuckets
from app.yougile import YouGileTask


TELEGRAM_MESSAGE_LIMIT = 4096
NO_TASKS = "На сегодня и до конца недели задач с дедлайнами нет 🎉"
NO_ASSIGNEES = "вы забыли написать, кто за это отвечает"
_B_TAG_RE = re.compile(r"</?b>")


@dataclass(frozen=True, slots=True)
class _TaskLine:
    number: int
    task: YouGileTask
    assignees: str | None

    def plain(self, title: str | None = None, assignees: str | None = None) -> str:
        rendered_title = self.task.title if title is None else title
        if self.assignees is None:
            return f"{self.number}. {rendered_title} — {NO_ASSIGNEES}"
        rendered_assignees = self.assignees if assignees is None else assignees
        return f"{self.number}. {rendered_title}: {rendered_assignees}"

    def render(self, max_visible_units: int = TELEGRAM_MESSAGE_LIMIT) -> str:
        original = self.plain()
        if _utf16_length(original) <= max_visible_units:
            return html.escape(original)

        # Prefer shortening only the API-provided task title.
        low, high = 0, len(self.task.title)
        best: str | None = None
        while low <= high:
            middle = (low + high) // 2
            shortened = self.task.title[:middle].rstrip() + "…"
            candidate = self.plain(title=shortened)
            if _utf16_length(candidate) <= max_visible_units:
                best = candidate
                low = middle + 1
            else:
                high = middle - 1
        if best is not None:
            return html.escape(best)

        # Extremely large assignee data is also bounded so Telegram never rejects the request.
        prefix = f"{self.number}. …"
        if self.assignees is None:
            return html.escape(_truncate_utf16(self.plain(title="…"), max_visible_units))
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
    max_length: int = TELEGRAM_MESSAGE_LIMIT,
) -> list[str]:
    intro = f"☀️ {html.escape(random.choice(DUMB_GREETINGS))}, коллеги!"
    sections = _build_sections(buckets, telegram_by_user_id, display_name_by_user_id)
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
) -> list[_Section]:
    specifications = (
        (
            buckets.today,
            f"Сегодня вам надо закрыть {format_task_count(len(buckets.today))}:",
        ),
        (
            buckets.week,
            "Помимо этого, до конца недели есть еще "
            f"{format_task_count(len(buckets.week))}:",
        ),
        (
            buckets.overdue,
            f"А еще вы просрочили {format_task_count(len(buckets.overdue))}! "
            "Буду тегать вас, пока не исправитесь:",
        ),
    )
    sections: list[_Section] = []
    for tasks, heading in specifications:
        if not tasks:
            continue
        lines = tuple(
            _TaskLine(
                number=index,
                task=task,
                assignees=format_assignees(
                    task.assigned, telegram_by_user_id, display_name_by_user_id
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
