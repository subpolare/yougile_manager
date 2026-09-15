from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.config import TELEGRAM_USERNAME_RE, VOICE_TASK_PROJECTS
from app.db import SessionFactory
from app.models import PersonalDigestSubscription


UNKNOWN = "Кажется, мы не знакомы. Попроси Влада занести тебя в белый список, и тогда я смогу тебе отвечать"
START = "Отлично! Теперь буду напоминать тебе о твоих задачах каждый будний день"
RETURNING = "С возвращением, и да прибудут с тобой дедлайны! Теперь я снова буду напоминать тебе о задачах"
STOP = "Хорошо, не буду присылать никакие уведомления, но ты всегда можешь взглянуть на свои актуальные дедлайны через /task. А если захочешь снова включить меня, нажми на /start"


class PersonalIdentity:
    def __init__(self, session_factory: SessionFactory, mappings: dict[str, str],
                 *, voice_task_employees: dict[str, str] | None = None) -> None:
        self.session_factory = session_factory
        self.mappings = mappings
        candidates: dict[str, set[str]] = {}
        for employee, username in (voice_task_employees or {}).items():
            project = VOICE_TASK_PROJECTS.get(employee)
            uid = self.lookup(username)
            if project and uid:
                candidates.setdefault(uid, set()).add(project)
        # Conflicting employee aliases must not select an arbitrary destination.
        self.voice_task_projects = {uid: next(iter(projects)) for uid, projects in candidates.items()
                                    if len(projects) == 1}

    async def voice_task_project(self, telegram_id: int, username: str | None) -> str | None:
        if not self.voice_task_projects:
            return None
        uid, _ = await self.resolve(telegram_id, username)
        return self.voice_task_projects.get(uid)

    def lookup(self, username: str | None) -> str | None:
        normalized = "@" + (username or "").removeprefix("@")
        if not TELEGRAM_USERNAME_RE.fullmatch(normalized):
            return None
        matches = {uid for uid, name in self.mappings.items()
                   if name.removeprefix("@").casefold() == normalized[1:].casefold()}
        return next(iter(matches)) if len(matches) == 1 else None

    async def resolve(
        self, telegram_id: int, username: str | None, *, action: str = "task"
    ) -> tuple[str | None, bool]:
        """Only start creates a row; bool indicates an existing subscription.

        Never transfer an established identity based on a recyclable username.
        Conflicting new identities are rejected, preserving the one-to-one map.
        """
        for attempt in range(2):
            try:
                async with self.session_factory() as session, session.begin():
                    row = await session.scalar(select(PersonalDigestSubscription).where(
                        PersonalDigestSubscription.telegram_user_id == telegram_id
                    ).with_for_update())
                    if row is not None:
                        row.telegram_username = username
                        if action in {"start", "stop"}:
                            row.enabled = action == "start"
                        return row.yougile_user_id, True
                    uid = self.lookup(username)
                    if uid is None:
                        return None, False
                    owner = await session.scalar(select(PersonalDigestSubscription).where(
                        PersonalDigestSubscription.yougile_user_id == uid
                    ))
                    if owner is not None:
                        return None, False
                    if action == "start":
                        session.add(PersonalDigestSubscription(
                            telegram_user_id=telegram_id, yougile_user_id=uid,
                            telegram_username=username, enabled=True,
                        ))
                        await session.flush()
                    return uid, False
            except IntegrityError:
                if attempt:
                    raise
        raise RuntimeError("Identity resolution failed")
