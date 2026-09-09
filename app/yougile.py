from __future__ import annotations

import asyncio
import email.utils
import logging
import math
import time
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx


logger = logging.getLogger(__name__)


class YouGileError(RuntimeError):
    """Base error for unavailable or invalid YouGile responses."""


class YouGileAPIError(YouGileError):
    pass


class YouGileDataError(YouGileError):
    pass


@dataclass(frozen=True, slots=True)
class YouGileProject:
    id: str
    title: str
    deleted: bool = False


@dataclass(frozen=True, slots=True)
class YouGileUser:
    id: str
    real_name: str


@dataclass(frozen=True, slots=True)
class YouGileBoard:
    id: str
    project_id: str
    deleted: bool = False


@dataclass(frozen=True, slots=True)
class YouGileColumn:
    id: str
    board_id: str
    deleted: bool = False


@dataclass(frozen=True, slots=True)
class YouGileTask:
    id: str
    title: str
    column_id: str | None
    deadline_ms: int | None
    assigned: tuple[str, ...] = ()
    completed: bool = False
    archived: bool = False
    deleted: bool = False


@dataclass(frozen=True, slots=True)
class WorkspaceSnapshot:
    boards: tuple[YouGileBoard, ...]
    columns: tuple[YouGileColumn, ...]
    tasks: tuple[YouGileTask, ...]
    users: tuple[YouGileUser, ...]

    def tasks_for_project(self, project_id: str) -> tuple[YouGileTask, ...]:
        board_ids = {
            board.id
            for board in self.boards
            if board.project_id == project_id and not board.deleted
        }
        column_ids = {
            column.id
            for column in self.columns
            if column.board_id in board_ids and not column.deleted
        }
        return tuple(task for task in self.tasks if task.column_id in column_ids)


class AsyncWindowRateLimiter:
    def __init__(self, max_requests: int = 45, period_seconds: float = 60.0) -> None:
        self.max_requests = max_requests
        self.period_seconds = period_seconds
        self._timestamps: deque[float] = deque()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            while True:
                now = time.monotonic()
                while self._timestamps and now - self._timestamps[0] >= self.period_seconds:
                    self._timestamps.popleft()
                if len(self._timestamps) < self.max_requests:
                    self._timestamps.append(now)
                    return
                await asyncio.sleep(self.period_seconds - (now - self._timestamps[0]) + 0.01)


class YouGileClient:
    """Async client for the documented YouGile REST API v2."""

    PAGE_SIZE = 1000

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = "https://yougile.com/api-v2",
        http_client: httpx.AsyncClient | None = None,
        rate_limiter: AsyncWindowRateLimiter | None = None,
    ) -> None:
        self._owns_client = http_client is None
        self._headers = {
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        self._client = http_client or httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=httpx.Timeout(20.0),
        )
        self._rate_limiter = rate_limiter or AsyncWindowRateLimiter()

    async def __aenter__(self) -> YouGileClient:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def _request_json(self, path: str, params: Mapping[str, Any]) -> Mapping[str, Any]:
        attempts = 4
        for attempt in range(attempts):
            await self._rate_limiter.acquire()
            try:
                response = await self._client.get(path, params=params, headers=self._headers)
            except httpx.HTTPError as exc:
                logger.warning("YouGile request failed path=%s error=%s", path, type(exc).__name__)
                if attempt == attempts - 1:
                    raise YouGileAPIError("YouGile request failed") from exc
                await asyncio.sleep(min(2**attempt, 8))
                continue

            if response.status_code == 429:
                if attempt == attempts - 1:
                    logger.error("YouGile rate limit persisted path=%s", path)
                    raise YouGileAPIError("YouGile rate limit exceeded")
                delay = _retry_after_seconds(response.headers.get("Retry-After"))
                logger.warning("YouGile rate limited path=%s retry_in=%.1fs", path, delay)
                await asyncio.sleep(delay)
                continue

            if response.status_code >= 500 and attempt < attempts - 1:
                logger.warning("YouGile server error path=%s status=%s", path, response.status_code)
                await asyncio.sleep(min(2**attempt, 8))
                continue

            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                logger.error("YouGile HTTP error path=%s status=%s", path, response.status_code)
                raise YouGileAPIError(f"YouGile returned HTTP {response.status_code}") from exc
            try:
                payload = response.json()
            except ValueError as exc:
                raise YouGileDataError("YouGile returned invalid JSON") from exc
            if not isinstance(payload, Mapping):
                raise YouGileDataError("YouGile response must be an object")
            return payload
        raise YouGileAPIError("YouGile request failed")

    async def _paginate(
        self, path: str, *, params: Mapping[str, Any] | None = None
    ) -> list[Mapping[str, Any]]:
        offset = 0
        result: list[Mapping[str, Any]] = []
        base_params = dict(params or {})
        while True:
            payload = await self._request_json(
                path, {**base_params, "limit": self.PAGE_SIZE, "offset": offset}
            )
            content = payload.get("content")
            paging = payload.get("paging")
            if not isinstance(content, list) or not isinstance(paging, Mapping):
                raise YouGileDataError("Paginated response lacks content or paging")
            if any(not isinstance(item, Mapping) for item in content):
                raise YouGileDataError("Paginated response contains a non-object item")
            result.extend(content)

            next_page = paging.get("next")
            page_count = paging.get("count")
            page_offset = paging.get("offset")
            page_limit = paging.get("limit")
            if not isinstance(next_page, bool):
                raise YouGileDataError("Pagination field 'next' must be boolean")
            if not all(_is_number(value) for value in (page_count, page_offset, page_limit)):
                raise YouGileDataError("Pagination count/offset/limit must be numeric")
            if float(page_count) < 0 or float(page_offset) < 0 or float(page_limit) <= 0:
                raise YouGileDataError("Pagination metadata is outside its valid range")
            if not next_page:
                return result
            new_offset = int(page_offset) + int(page_limit)
            if new_offset <= offset:
                raise YouGileDataError("Pagination did not advance")
            offset = new_offset

    async def fetch_projects(self) -> list[YouGileProject]:
        rows = await self._paginate("/projects", params={"includeDeleted": False})
        return [_parse_project(row) for row in rows]

    async def fetch_users(self) -> list[YouGileUser]:
        rows = await self._paginate("/users")
        return [_parse_user(row) for row in rows]

    async def fetch_workspace(self) -> WorkspaceSnapshot:
        boards_rows, columns_rows, tasks_rows, users_rows = await asyncio.gather(
            self._paginate("/boards", params={"includeDeleted": False}),
            self._paginate("/columns", params={"includeDeleted": False}),
            self._paginate("/task-list", params={"includeDeleted": False}),
            self._paginate("/users"),
        )
        return WorkspaceSnapshot(
            boards=tuple(_parse_board(row) for row in boards_rows),
            columns=tuple(_parse_column(row) for row in columns_rows),
            tasks=tuple(_parse_task(row) for row in tasks_rows),
            users=tuple(_parse_user(row) for row in users_rows),
        )


def _retry_after_seconds(value: str | None) -> float:
    if not value:
        return 2.0
    try:
        return max(0.0, min(float(value), 60.0))
    except ValueError:
        try:
            parsed = email.utils.parsedate_to_datetime(value)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=UTC)
            return max(0.0, min((parsed - datetime.now(UTC)).total_seconds(), 60.0))
        except (TypeError, ValueError):
            return 2.0


def _is_number(value: object) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )


def _required_string(row: Mapping[str, Any], field: str, entity: str) -> str:
    value = row.get(field)
    if not isinstance(value, str) or not value:
        raise YouGileDataError(f"{entity}.{field} must be a non-empty string")
    return value


def _optional_bool(row: Mapping[str, Any], field: str) -> bool:
    value = row.get(field, False)
    # The current API serializes optional active-state flags as null.
    if value is None:
        return False
    if not isinstance(value, bool):
        raise YouGileDataError(f"{field} must be boolean")
    return value


def _parse_project(row: Mapping[str, Any]) -> YouGileProject:
    return YouGileProject(
        id=_required_string(row, "id", "project"),
        title=_required_string(row, "title", "project"),
        deleted=_optional_bool(row, "deleted"),
    )


def _parse_user(row: Mapping[str, Any]) -> YouGileUser:
    return YouGileUser(
        id=_required_string(row, "id", "user"),
        real_name=_required_string(row, "realName", "user"),
    )


def _parse_board(row: Mapping[str, Any]) -> YouGileBoard:
    return YouGileBoard(
        id=_required_string(row, "id", "board"),
        project_id=_required_string(row, "projectId", "board"),
        deleted=_optional_bool(row, "deleted"),
    )


def _parse_column(row: Mapping[str, Any]) -> YouGileColumn:
    return YouGileColumn(
        id=_required_string(row, "id", "column"),
        board_id=_required_string(row, "boardId", "column"),
        deleted=_optional_bool(row, "deleted"),
    )


def _parse_task(row: Mapping[str, Any]) -> YouGileTask:
    column_id = row.get("columnId")
    if column_id is not None and not isinstance(column_id, str):
        raise YouGileDataError("task.columnId must be a string")

    assigned_raw = row.get("assigned")
    if assigned_raw is None:
        assigned_raw = []
    if not isinstance(assigned_raw, list) or any(
        not isinstance(user_id, str) for user_id in assigned_raw
    ):
        raise YouGileDataError("task.assigned must be an array of strings")

    deadline_ms: int | None = None
    deadline_raw = row.get("deadline")
    if deadline_raw is not None:
        if not isinstance(deadline_raw, Mapping):
            raise YouGileDataError("task.deadline must be an object")
        deadline_value = deadline_raw.get("deadline")
        if not _is_number(deadline_value):
            raise YouGileDataError("task.deadline.deadline must be numeric")
        deadline_ms = int(deadline_value)
        try:
            datetime.fromtimestamp(deadline_ms / 1000, tz=UTC)
        except (OverflowError, OSError, ValueError) as exc:
            raise YouGileDataError("task.deadline.deadline is outside datetime range") from exc

    return YouGileTask(
        id=_required_string(row, "id", "task"),
        title=_required_string(row, "title", "task"),
        column_id=column_id,
        deadline_ms=deadline_ms,
        assigned=tuple(assigned_raw),
        completed=_optional_bool(row, "completed"),
        archived=_optional_bool(row, "archived"),
        deleted=_optional_bool(row, "deleted"),
    )
