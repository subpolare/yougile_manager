from __future__ import annotations

from app.task_service import is_eligible_project, match_projects
from app.yougile import YouGileProject


def project(project_id: str, title: str, *, deleted: bool = False) -> YouGileProject:
    return YouGileProject(id=project_id, title=title, deleted=deleted)


def test_numeric_init_matches_exact_number_prefix() -> None:
    projects = [project("7", "#7 Проект"), project("70", "#70 Другой")]
    assert match_projects(projects, "7") == [projects[0]]


def test_full_title_requires_exact_match_after_trimming_argument() -> None:
    projects = [project("7", "#7 Агрегатные состояния / Отдел")]
    assert match_projects(projects, "  #7 Агрегатные состояния / Отдел  ") == [projects[0]]
    assert match_projects(projects, "#7 Агрегатные состояния") == []


def test_head_project_is_always_excluded() -> None:
    excluded = project("5", "#5 Голова компании")
    assert not is_eligible_project(excluded)
    assert match_projects([excluded], "5") == []
    assert match_projects([excluded], excluded.title) == []


def test_ambiguous_numeric_match_returns_all_exact_number_matches() -> None:
    projects = [project("a", "#7 Первый"), project("b", "#7 Второй")]
    assert match_projects(projects, "7") == projects


def test_ineligible_and_deleted_projects_are_ignored() -> None:
    projects = [project("plain", "Проект #7"), project("deleted", "#7 Удалён", deleted=True)]
    assert match_projects(projects, "7") == []

