from __future__ import annotations

import re
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from app.formatter import format_digest, telegram_visible_length
from app.task_service import TaskBuckets, project_buckets
from app.yougile import WorkspaceSnapshot, YouGileBoard, YouGileColumn, YouGileTask


TODAY = date(2026, 9, 9)
MOSCOW = ZoneInfo("Europe/Moscow")


def timestamp_ms(day: int, hour: int = 12) -> int:
    return int(datetime(2026, 9, day, hour, tzinfo=MOSCOW).timestamp() * 1000)


def task(
    task_id: str,
    title: str,
    *,
    column_id: str | None = None,
    deadline_ms: int | None = None,
    assigned: tuple[str, ...] = (),
    subtask_ids: tuple[str, ...] = (),
    completed: bool = False,
    archived: bool = False,
    deleted: bool = False,
) -> YouGileTask:
    return YouGileTask(
        id=task_id,
        title=title,
        column_id=column_id,
        deadline_ms=deadline_ms,
        assigned=assigned,
        completed=completed,
        archived=archived,
        deleted=deleted,
        subtask_ids=subtask_ids,
    )


def snapshot(
    tasks: tuple[YouGileTask, ...],
    *,
    columns: tuple[YouGileColumn, ...] | None = None,
) -> WorkspaceSnapshot:
    project_columns = columns or (
        YouGileColumn(
            id="postproduction",
            board_id="board",
            title="Постпродакшн",
            display_order=0,
        ),
    )
    return WorkspaceSnapshot(
        boards=(
            YouGileBoard(
                id="board",
                project_id="project",
                title="Основная доска",
                display_order=0,
            ),
            YouGileBoard(
                id="other-board",
                project_id="other-project",
                title="Чужая доска",
                display_order=0,
            ),
        ),
        columns=project_columns,
        tasks=tasks,
        users=(),
    )


def bucket_ids(buckets: TaskBuckets) -> tuple[list[str], list[str], list[str]]:
    return (
        [item.id for item in buckets.today],
        [item.id for item in buckets.week],
        [item.id for item in buckets.overdue],
    )


def test_parent_and_subtask_are_independent_numbered_items_with_own_assignees(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("app.formatter.random.choice", lambda _: "С добрым утром")
    workspace = snapshot(
        (
            task(
                "parent",
                "Рыба выпуска",
                column_id="postproduction",
                deadline_ms=timestamp_ms(9, 9),
                assigned=("vlad",),
                subtask_ids=("child",),
            ),
            task(
                "child",
                "Нарезать ГЗК",
                deadline_ms=timestamp_ms(9, 10),
                assigned=("misha",),
            ),
        )
    )
    buckets = project_buckets(workspace, "project", today=TODAY)
    rendered = format_digest(
        buckets,
        {"vlad": "@vlad", "misha": "@misha"},
        {},
        today=TODAY,
    )[0]

    assert [item.id for item in buckets.today] == ["parent", "child"]
    assert buckets.today[1].parent_task_id == "parent"
    assert buckets.today[1].parent_task_title == "Рыба выпуска"
    assert "закрыть 2 задачи" in rendered
    assert "1. Постпродакшн. Рыба выпуска: @vlad" in rendered
    assert (
        "2. Постпродакшн. Нарезать ГЗК "
        "(подзадача внутри «Рыба выпуска»): @misha"
    ) in rendered


def test_subtask_uses_own_deadline_and_can_land_in_a_different_bucket() -> None:
    workspace = snapshot(
        (
            task(
                "parent",
                "Родитель",
                column_id="postproduction",
                deadline_ms=timestamp_ms(9),
                subtask_ids=("child",),
            ),
            task("child", "Подзадача", deadline_ms=timestamp_ms(10)),
        )
    )

    buckets = project_buckets(workspace, "project", today=TODAY)

    assert bucket_ids(buckets) == (["parent"], ["child"], [])


def test_subtask_without_deadline_is_excluded_and_does_not_inherit_parent_deadline() -> None:
    workspace = snapshot(
        (
            task(
                "parent",
                "Родитель",
                column_id="postproduction",
                deadline_ms=timestamp_ms(9),
                subtask_ids=("child",),
            ),
            task("child", "Без дедлайна"),
        )
    )

    buckets = project_buckets(workspace, "project", today=TODAY)

    assert bucket_ids(buckets) == (["parent"], [], [])


def test_subtask_deadline_annotations_use_special_compact_rendering(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("app.formatter.random.choice", lambda _: "С добрым утром")
    workspace = snapshot(
        (
            task(
                "parent",
                "Рыба выпуска",
                column_id="postproduction",
                subtask_ids=(
                    "tomorrow",
                    "day-after-tomorrow",
                    "later-week",
                    "yesterday",
                    "day-before-yesterday",
                    "older-overdue",
                ),
            ),
            task(
                "tomorrow",
                "Записать ГЗК",
                deadline_ms=timestamp_ms(10),
                assigned=("user",),
            ),
            task(
                "day-after-tomorrow",
                "Бриф по графике",
                deadline_ms=timestamp_ms(11),
                assigned=("user",),
            ),
            task(
                "later-week",
                "Проверить текст",
                deadline_ms=timestamp_ms(13),
                assigned=("user",),
            ),
            task(
                "yesterday",
                "Проверить факты",
                deadline_ms=timestamp_ms(8),
                assigned=("user",),
            ),
            task(
                "day-before-yesterday",
                "Проверить источники",
                deadline_ms=timestamp_ms(7),
                assigned=("user",),
            ),
            task(
                "older-overdue",
                "Добавить ссылки",
                deadline_ms=timestamp_ms(3),
                assigned=("user",),
            ),
        )
    )
    buckets = project_buckets(workspace, "project", today=TODAY)
    rendered = format_digest(buckets, {"user": "@user"}, {}, today=TODAY)[0]

    assert (
        "Записать ГЗК (подзадача внутри «Рыба выпуска»), "
        "до завтра, 10.09: @user"
    ) in rendered
    assert (
        "Бриф по графике (подзадача внутри «Рыба выпуска»), "
        "до послезавтра, 11.09: @user"
    ) in rendered
    assert (
        "Проверить текст (подзадача внутри «Рыба выпуска»), "
        "до 13.09: @user"
    ) in rendered
    assert (
        "Проверить факты (подзадача внутри «Рыба выпуска»), "
        "дедлайн вчера, 08.09.2026: @user"
    ) in rendered
    assert (
        "Проверить источники (подзадача внутри «Рыба выпуска»), "
        "дедлайн позавчера, 07.09.2026: @user"
    ) in rendered
    assert (
        "Добавить ссылки (подзадача внутри «Рыба выпуска»), "
        "дедлайн 03.09.2026: @user"
    ) in rendered
    future_section = rendered.split("<b>А еще вы просрочили", maxsplit=1)[0]
    assert ".2026" not in future_section


def test_today_subtask_without_assignee_uses_fallback_and_not_parent_assignee(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("app.formatter.random.choice", lambda _: "С добрым утром")
    workspace = snapshot(
        (
            task(
                "parent",
                "Рыба выпуска",
                column_id="postproduction",
                assigned=("vlad",),
                subtask_ids=("child",),
            ),
            task("child", "Собрать монтаж", deadline_ms=timestamp_ms(9)),
        )
    )
    buckets = project_buckets(workspace, "project", today=TODAY)
    rendered = format_digest(buckets, {}, {}, today=TODAY)[0]

    assert (
        "Постпродакшн. Собрать монтаж "
        "(подзадача внутри «Рыба выпуска») — "
        "вы забыли написать, кто за это отвечает"
    ) in rendered
    assert "@vlad" not in rendered
    assert "дедлайн" not in rendered


def test_subtask_and_parent_titles_are_html_escaped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("app.formatter.random.choice", lambda _: "С добрым утром")
    workspace = snapshot(
        (
            task(
                "parent",
                "Рыба <выпуска> & монтаж",
                column_id="postproduction",
                subtask_ids=("child",),
            ),
            task(
                "child",
                "<Бриф> & графика",
                deadline_ms=timestamp_ms(9),
                assigned=("user",),
            ),
        )
    )
    buckets = project_buckets(workspace, "project", today=TODAY)
    rendered = format_digest(buckets, {"user": "@user"}, {}, today=TODAY)[0]

    assert (
        "Постпродакшн. &lt;Бриф&gt; &amp; графика "
        "(подзадача внутри «Рыба &lt;выпуска&gt; "
        "&amp; монтаж»): @user"
    ) in rendered
    assert "<Бриф>" not in rendered


@pytest.mark.parametrize("status", ["completed", "archived", "deleted"])
def test_inactive_subtask_is_excluded(status: str) -> None:
    child_status = {status: True}
    workspace = snapshot(
        (
            task(
                "parent",
                "Родитель",
                column_id="postproduction",
                subtask_ids=("child",),
            ),
            task("child", "Не показывать", deadline_ms=timestamp_ms(9), **child_status),
        )
    )

    buckets = project_buckets(workspace, "project", today=TODAY)

    assert bucket_ids(buckets) == ([], [], [])


def test_open_subtask_is_not_hidden_by_completed_parent() -> None:
    workspace = snapshot(
        (
            task(
                "parent",
                "Готовый родитель",
                column_id="postproduction",
                deadline_ms=timestamp_ms(9),
                completed=True,
                subtask_ids=("child",),
            ),
            task("child", "Открытая подзадача", deadline_ms=timestamp_ms(9)),
        )
    )

    buckets = project_buckets(workspace, "project", today=TODAY)

    assert bucket_ids(buckets) == (["child"], [], [])


def test_parents_and_subtasks_share_column_and_deadline_sorting() -> None:
    columns = (
        YouGileColumn(
            id="editorial",
            board_id="board",
            title="Редакция",
            display_order=0,
        ),
        YouGileColumn(
            id="design",
            board_id="board",
            title="Дизайн",
            display_order=1,
        ),
    )
    workspace = snapshot(
        (
            task(
                "container",
                "Контейнер",
                column_id="editorial",
                subtask_ids=("later-child", "earlier-child"),
            ),
            task("later-child", "Позже", deadline_ms=timestamp_ms(9, 18)),
            task("earlier-child", "Раньше", deadline_ms=timestamp_ms(9, 8)),
            task(
                "design-parent",
                "Дизайн раньше по времени",
                column_id="design",
                deadline_ms=timestamp_ms(9, 7),
            ),
        ),
        columns=columns,
    )

    buckets = project_buckets(workspace, "project", today=TODAY)

    assert [item.id for item in buckets.today] == [
        "earlier-child",
        "later-child",
        "design-parent",
    ]


def test_subtask_present_as_task_list_item_and_parent_reference_is_not_duplicated() -> None:
    workspace = snapshot(
        (
            task(
                "child",
                "Подзадача",
                column_id="postproduction",
                deadline_ms=timestamp_ms(9),
            ),
            task(
                "parent",
                "Родитель",
                column_id="postproduction",
                deadline_ms=timestamp_ms(9),
                subtask_ids=("child",),
            ),
        )
    )

    buckets = project_buckets(workspace, "project", today=TODAY)

    assert [item.id for item in buckets.today].count("child") == 1
    assert set(item.id for item in buckets.today) == {"parent", "child"}


def test_nested_subtasks_are_recursive_and_cycle_safe() -> None:
    workspace = snapshot(
        (
            task(
                "parent",
                "Родитель",
                column_id="postproduction",
                deadline_ms=timestamp_ms(9, 8),
                subtask_ids=("child",),
            ),
            task(
                "child",
                "Дочерняя",
                deadline_ms=timestamp_ms(9, 9),
                subtask_ids=("grandchild",),
            ),
            task(
                "grandchild",
                "Внучатая",
                deadline_ms=timestamp_ms(9, 10),
                subtask_ids=("parent", "missing", "grandchild"),
            ),
        )
    )

    buckets = project_buckets(workspace, "project", today=TODAY)

    assert [item.id for item in buckets.today] == ["parent", "child", "grandchild"]
    assert buckets.today[1].parent_task_title == "Родитель"
    assert buckets.today[2].parent_task_id == "child"
    assert buckets.today[2].parent_task_title == "Дочерняя"


def test_nested_subtask_renders_its_immediate_parent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("app.formatter.random.choice", lambda _: "С добрым утром")
    workspace = snapshot(
        (
            task(
                "grandparent",
                "Task A",
                column_id="postproduction",
                subtask_ids=("parent",),
            ),
            task("parent", "Subtask B", subtask_ids=("child",)),
            task("child", "Subtask C", deadline_ms=timestamp_ms(9)),
        )
    )
    buckets = project_buckets(workspace, "project", today=TODAY)
    rendered = format_digest(buckets, {}, {}, today=TODAY)[0]

    assert "Subtask C (подзадача внутри «Subtask B»)" in rendered
    assert "Subtask C (подзадача внутри «Task A»)" not in rendered


def test_explicit_column_from_another_project_prevents_subtask_leak() -> None:
    columns = (
        YouGileColumn(
            id="postproduction",
            board_id="board",
            title="Постпродакшн",
            display_order=0,
        ),
        YouGileColumn(
            id="other-column",
            board_id="other-board",
            title="Чужая колонка",
            display_order=0,
        ),
    )
    workspace = snapshot(
        (
            task(
                "parent",
                "Родитель",
                column_id="postproduction",
                deadline_ms=timestamp_ms(9),
                subtask_ids=("foreign",),
            ),
            task(
                "foreign",
                "Чужая задача",
                column_id="other-column",
                deadline_ms=timestamp_ms(9),
            ),
        ),
        columns=columns,
    )

    buckets = project_buckets(workspace, "project", today=TODAY)

    assert [item.id for item in buckets.today] == ["parent"]


def test_semantic_splitting_handles_many_flattened_subtasks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("app.formatter.random.choice", lambda _: "С добрым утром")
    child_ids = tuple(f"child-{index}" for index in range(1, 16))
    long_parent = "Очень длинный родительский заголовок " * 300
    tasks = (
        task(
            "parent",
            long_parent,
            column_id="postproduction",
            subtask_ids=child_ids,
        ),
        *(
            task(
                child_id,
                f"Самостоятельная подзадача {index} "
                + "с длинным названием " * 80,
                deadline_ms=timestamp_ms(9, 9),
            )
            for index, child_id in enumerate(child_ids, start=1)
        ),
    )
    buckets = project_buckets(snapshot(tasks), "project", today=TODAY)
    chunks = format_digest(buckets, {}, {}, today=TODAY, max_length=210)

    numbering = [
        int(match)
        for chunk in chunks
        for match in re.findall(r"(?m)^(\d+)\. ", chunk)
    ]
    assert numbering == list(range(1, 16))
    greeting_chunks = sum(
        "☀️ С добрым утром, коллеги!" in chunk for chunk in chunks
    )
    assert greeting_chunks == 1
    assert all(telegram_visible_length(chunk) <= 210 for chunk in chunks)
    assert all("подзадача внутри «" in chunk for chunk in chunks[1:])
    assert "…" in "".join(chunks)
