from dataclasses import replace
from datetime import date, datetime, timedelta
from html.parser import HTMLParser

import pytest

from app.formatter import format_digest, telegram_visible_length
from app.personal_digest import (
    EMPTY, FOOTER, format_personal_digest, overdue_heading,
    personal_buckets, personal_project_name, personal_task_index,
)
from app.task_service import MOSCOW_TZ, TaskBuckets
from app.yougile import WorkspaceSnapshot, YouGileBoard, YouGileColumn, YouGileProject, YouGileTask


TODAY = date(2026, 9, 9)
GREETING = "Попутного ветра и спокойного моря"


def item(uid="task", *, day=9, hour=12, **kwargs):
    defaults = dict(id=uid, title=uid, column_id="c", assigned=("u",),
                    deadline_ms=int(datetime(2026, 9, day, hour, tzinfo=MOSCOW_TZ).timestamp()*1000))
    return YouGileTask(**(defaults | kwargs))


def workspace(tasks, titles=("#7 Эволюция / Монтаж", "ТОПКАСТ", "#23 Рак / Продакшн")):
    return WorkspaceSnapshot(
        projects=tuple(YouGileProject(f"p{i}", title) for i, title in enumerate(titles)),
        boards=tuple(YouGileBoard(f"b{i}", f"p{i}") for i in range(len(titles))),
        columns=tuple(YouGileColumn("c" if i == 0 else f"c{i}", f"b{i}", "КОЛОНКА")
                      for i in range(len(titles))), tasks=tuple(tasks), users=(),
    )


@pytest.mark.parametrize("title,expected", [
    ("#7 Эволюция / Монтаж", "#7 Эволюция"),
    ("#12 Космос/Продакшн", "#12 Космос"), ("#3 Вирусы", "#3 Вирусы"),
    ("  #7 Эволюция / Монтаж  ", "#7 Эволюция"),
    ("  ТОПКАСТ / Монтаж  ", "ТОПКАСТ / Монтаж"),
    ("#5 Голова / A", None), ("#15 Шифры / A", None),
    ("#19 Растения / A", None), ("#23 Рак / A", None),
    ("#1 рАк / A", None), ("#23 Ракообразные / A", "#23 Ракообразные"),
    ("#23 (Рак) / A", None), ("#7 Эволюция / Рак", "#7 Эволюция"),
    ("Рак / A", "Рак / A"),
])
def test_personal_project_normalization(title, expected):
    assert personal_project_name(YouGileProject("p", title)) == expected


def test_aggregation_all_projects_own_assignees_and_duplicate_ids():
    parent = item("parent", assigned=("other",), subtask_ids=("child", "no-date", "other-child"))
    child = item("child", column_id=None, subtask_ids=("nested",))
    snapshot = workspace([
        parent, child, item("nested", column_id=None),
        item("no-date", column_id=None, deadline_ms=None),
        item("other-child", column_id=None, assigned=("other",)),
        item("ordinary"), item("ordinary"), item("multi", assigned=("other", "u", "u")),
        item("topcast", column_id="c1"),
        item("excluded", column_id="c2", subtask_ids=("excluded-child",)),
        item("excluded-child", column_id=None), item("only-other", assigned=("other",)),
        item("no-assignee", assigned=()),
    ])
    tasks = personal_task_index(snapshot)["u"]
    assert {t.id for t in tasks} == {"child", "nested", "ordinary", "multi", "topcast"}
    assert len(tasks) == 5
    nested = next(t for t in tasks if t.id == "nested")
    assert nested.parent_task_id == "child" and nested.parent_task_title == "child"
    assert next(t for t in tasks if t.id == "topcast").personal_project_title == "ТОПКАСТ"


@pytest.mark.parametrize("state", [dict(completed=True), dict(archived=True),
                                   dict(deleted=True), dict(deadline_ms=None)])
@pytest.mark.parametrize("subtask", [False, True])
def test_item_own_state_and_deadline_required(state, subtask):
    tasks = [item("target", column_id=None if subtask else "c", **state)]
    if subtask:
        tasks.append(item("parent", assigned=("other",), subtask_ids=("target",)))
    assert "u" not in personal_task_index(workspace(tasks))


def test_parent_assignee_not_inherited_and_parent_state_not_inherited():
    snapshot = workspace([
        item("mine", subtask_ids=("theirs",)),
        item("theirs", assigned=("other",), column_id=None),
        item("done-parent", completed=True, archived=True, deleted=True, assigned=("other",),
             subtask_ids=("active-child",)),
        item("active-child", column_id=None),
    ])
    assert {t.id for t in personal_task_index(snapshot)["u"]} == {"mine", "active-child"}


@pytest.mark.parametrize("entity", ["project", "board", "column"])
def test_deleted_container_excluded(entity):
    snapshot = workspace([item()])
    field = {"project": "projects", "board": "boards", "column": "columns"}[entity]
    snapshot = replace(snapshot, **{field: tuple(replace(x, deleted=True) for x in getattr(snapshot, field))})
    assert not personal_task_index(snapshot)


@pytest.mark.parametrize("count,expected", [
    (1, "просрочена 1 задача"), (2, "просрочены 2 задачи"), (4, "просрочены 4 задачи"),
    (5, "просрочено 5 задач"), (11, "просрочено 11 задач"), (21, "просрочена 21 задача"),
    (22, "просрочены 22 задачи"), (25, "просрочено 25 задач"), (114, "просрочено 114 задач"),
])
def test_overdue_grammar(count, expected):
    assert overdue_heading(count) == f"А еще у тебя {expected}:"


@pytest.mark.parametrize("day,annotation", [
    (9, None), (10, "до завтра, 10.09"), (11, "до послезавтра, 11.09"),
    (13, "до 13.09"), (8, "дедлайн вчера, 08.09.2026"),
    (7, "дедлайн позавчера, 07.09.2026"), (3, "дедлайн 03.09.2026"),
])
@pytest.mark.parametrize("subtask", [False, True])
def test_exact_item_format(day, annotation, subtask):
    task = item(title="Текст <&>", day=day, personal_project_title="#7 <Эволюция>",
                column_title="НЕ ПОКАЗЫВАТЬ", parent_task_id="parent" if subtask else None,
                parent_task_title="Рыба <&>" if subtask else None)
    text = format_personal_digest(personal_buckets((task,), TODAY), today=TODAY, greeting=GREETING)[0]
    expected = "1. #7 &lt;Эволюция&gt;. Текст &lt;&amp;&gt;"
    if subtask:
        expected += " (подзадача внутри «Рыба &lt;&amp;&gt;»)"
    if annotation:
        expected += f", {annotation}" if subtask else f" ({annotation})"
    assert expected in text.splitlines()
    assert "НЕ ПОКАЗЫВАТЬ" not in text and "@" not in text and "отвечает" not in text
    assert text.startswith(f"☀️ {GREETING}, коллеги!\n\n<b>")
    assert text.endswith("\n\n" + FOOTER)
    if day == 9:
        assert "<b>Сегодня среда, 9 сентября, и лично тебе надо закрыть 1 задачу:</b>" in text
    if day > 9:
        assert "<b>Помимо этого, до конца недели есть еще 1 задача:</b>" in text
        assert "2026" not in text


def test_exact_empty_and_group_has_no_footer():
    buckets = TaskBuckets((), (), ())
    assert format_personal_digest(buckets, today=TODAY, greeting=GREETING) == [
        f"☀️ {GREETING}, коллеги!\n\n{EMPTY}\n\n{FOOTER}"
    ]
    assert FOOTER not in format_digest(buckets, {}, {}, today=TODAY, greeting=GREETING)[0]


def test_date_first_sorting_ties_and_disjoint_buckets():
    tasks = (
        item("today-late", hour=20, board_order=0), item("today-early", hour=1, board_order=9),
        item("week-late", day=13), item("week-early", day=10),
        item("overdue-new", day=8), item("overdue-old", day=1),
        item("tie-z", day=1, title="A", personal_project_title="B"),
        item("tie-b", day=1, title="B", personal_project_title="A"),
        item("tie-a2", day=1, title="A", personal_project_title="A", parent_task_id="parent"),
        item("tie-a1", day=1, title="A", personal_project_title="A"),
        item("next-week", day=14),
    )
    buckets = personal_buckets(tasks, TODAY)
    assert [x.id for x in buckets.today] == ["today-early", "today-late"]
    assert [x.id for x in buckets.week] == ["week-early", "week-late"]
    assert [x.id for x in buckets.overdue] == [
        "overdue-old", "tie-a1", "tie-a2", "tie-b", "tie-z", "overdue-new"
    ]


def test_moscow_midnight_and_sunday_cutoff():
    sunday = date(2026, 9, 13)
    tasks = (item("sunday", day=13, hour=23), item("monday", day=14, hour=0))
    buckets = personal_buckets(tasks, sunday)
    assert [t.id for t in buckets.today] == ["sunday"]
    assert not buckets.week


class ValidHTML(HTMLParser):
    def __init__(self):
        super().__init__()
        self.stack = []

    def handle_starttag(self, tag, attrs):
        self.stack.append(tag)

    def handle_endtag(self, tag):
        assert self.stack.pop() == tag


def test_semantic_splitting_numbering_footer_and_extreme_titles():
    tasks = tuple(item(str(i), title=f"TASK-{i} " + "<&😀>" * 80,
                       personal_project_title="#7 Проект") for i in range(80))
    huge = item("huge", title="😀<&" * 5000, personal_project_title="Проект😀" * 5000,
                parent_task_id="p", parent_task_title="Родитель<&😀" * 5000)
    chunks = format_personal_digest(personal_buckets(tasks + (huge,), TODAY), today=TODAY, greeting=GREETING)
    assert len(chunks) > 1
    assert sum(GREETING in chunk for chunk in chunks) == 1 and GREETING in chunks[0]
    assert sum(FOOTER in chunk for chunk in chunks) == 1 and chunks[-1].endswith(FOOTER)
    text = "\n".join(chunks)
    for n in range(1, 82):
        assert len([line for line in text.splitlines() if line.startswith(f"{n}. ")]) == 1
    assert "подзадача внутри «" in text
    for chunk in chunks:
        assert telegram_visible_length(chunk) <= 4096
        parser = ValidHTML()
        parser.feed(chunk)
        assert not parser.stack
        if "<b>" in chunk:
            assert "1. " in chunk
