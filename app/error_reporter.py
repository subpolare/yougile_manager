from __future__ import annotations

import logging
import re
import traceback
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import func, select

from app.models import ErrorAdminTarget, PersonalDigestSubscription

logger = logging.getLogger(__name__)


class Sanitizer:
    def __init__(self, secrets=()):
        self.secrets = tuple(str(value) for value in secrets if value)

    def clean(self, value: str) -> str:
        for secret in sorted(self.secrets, key=len, reverse=True):
            value = value.replace(secret, "[REDACTED]")
        for pattern in (
            r"(?i)Bearer\s+[^\s,;\"']+",
            r"\b\d{5,}:[A-Za-z0-9_-]{20,}",
            r"\bsk-[A-Za-z0-9_-]+",
            r"\b[a-zA-Z][a-zA-Z0-9+.-]*://[^\s<>\"']+",
            r"(?i)(?:authorization|password|passwd|api[_-]?key|token|secret|yougile[_-]?key)\s*[:=]\s*[^\s,;]+",
        ):
            value = re.sub(pattern, "[REDACTED]", value)
        return value[:3000]

    def exception(self, exc: BaseException) -> str:
        # Never stringify arbitrary exceptions: SDK/SQLAlchemy/validation errors may
        # contain complete bodies, parameter values, transcript or reminder text.
        name = type(exc).__name__
        status = getattr(exc, "status_code", None)
        errno = getattr(exc, "errno", None)
        meta = []
        if isinstance(status, int):
            meta.append(f"HTTP {status}")
        if isinstance(errno, int):
            meta.append(f"errno {errno}")
        return self.clean(name + (": " + ", ".join(meta) if meta else ": детали скрыты для защиты данных"))


class PrivacyLogFilter(logging.Filter):
    """Last defense for third-party exception/polling logs; never print traceback locals."""
    def __init__(self, sanitizer):
        super().__init__()
        self.sanitizer = sanitizer

    def filter(self, record):
        if record.exc_info:
            record.msg = f"Runtime failure: {self.sanitizer.exception(record.exc_info[1])}"
            record.args = ()
            record.exc_info = None
            record.exc_text = None
        record.msg = self.sanitizer.clean(record.getMessage())
        record.args = ()
        return True


class ErrorReporter:
    def __init__(self, bot, session_factory, openai_service, *, admin_id=None, secrets=()):
        self.bot = bot
        self.session_factory = session_factory
        self.openai = openai_service
        self.admin_id = admin_id
        self.explicit_admin_id = admin_id
        self.sanitizer = Sanitizer(secrets)

    async def resolve_admin(self):
        if self.admin_id:
            return self.admin_id
        async with self.session_factory() as session, session.begin():
            target = await session.get(ErrorAdminTarget, "subpolare")
            if target:
                self.admin_id = target.telegram_user_id
            else:
                self.admin_id = await session.scalar(select(PersonalDigestSubscription.telegram_user_id).where(
                    func.lower(func.replace(PersonalDigestSubscription.telegram_username, "@", "")) == "subpolare"
                ))
                if self.admin_id is not None:
                    session.add(ErrorAdminTarget(identity="subpolare", telegram_user_id=self.admin_id))
        return self.admin_id

    async def observe_admin(self, user):
        if user is None or (user.username or "").removeprefix("@").casefold() != "subpolare":
            return
        # Once learned, keep the numeric identity authoritative over recyclable usernames.
        known = await self.resolve_admin()
        if known is not None and known != user.id:
            return
        async with self.session_factory() as session, session.begin():
            await session.merge(ErrorAdminTarget(identity="subpolare", telegram_user_id=user.id))
        self.admin_id = self.explicit_admin_id or user.id

    async def startup_check(self):
        try:
            target = await self.resolve_admin()
        except Exception as exc:
            logger.critical("Cannot resolve error administrator: %s", self.sanitizer.exception(exc))
            return
        if target is None:
            logger.critical("ERROR ADMIN UNRESOLVED: configure ERROR_ADMIN_TELEGRAM_USER_ID or ask @subpolare to message the bot privately")
        else:
            logger.info("Numeric error administrator target resolved")

    async def report(self, exc: BaseException, *, component: str, operation: str, **ids):
        original = self.sanitizer.exception(exc)
        frames = traceback.extract_tb(exc.__traceback__)[-5:]
        # No source lines, locals, raw exception repr, request/response objects or arbitrary context.
        trace = " > ".join(f"{Path(f.filename).name}:{f.lineno}:{f.name}" for f in frames)
        safe_ids = {key: value for key, value in ids.items()
                    if key in {"telegram_user_id", "telegram_chat_id", "reminder_id"}
                    and type(value) is int}
        context = self.sanitizer.clean(
            f"timestamp={datetime.now(timezone.utc).isoformat()}\ncomponent={component}\n"
            f"operation={operation}\nerror={original}\nids={safe_ids}\ntrace={trace}"
        )
        logger.error("Technical failure %s", context)
        try:
            explanation = self.sanitizer.clean(await self.openai.explain_error(context))
            alert = (f"🚨 Ошибка в боте\n\n{explanation}\n\n"
                     f"Компонент: {component}\nОшибка: {type(exc).__name__}\nОперация: {operation}")
        except Exception as terra_exc:
            terra = self.sanitizer.exception(terra_exc)
            logger.error("Terra explanation failed: %s", terra)
            alert = (f"🚨 Ошибка в боте\n\nОсновная ошибка:\n{original}\n"
                     f"Компонент: {component}\nОперация: {operation}\n\n"
                     f"⚠️ Terra тоже не ответила:\n{terra}\n\n"
                     "Поэтому автоматическое пояснение ошибки недоступно.")
        try:
            admin_id = await self.resolve_admin()
            if admin_id is None:
                logger.critical("ERROR ADMIN UNRESOLVED; alert not delivered; configure ERROR_ADMIN_TELEGRAM_USER_ID")
                return
            await self.bot.send_message(chat_id=admin_id, text=self.sanitizer.clean(alert), parse_mode=None)
        except Exception as send_exc:
            logger.error("Admin notification failed: %s; original=%s",
                         self.sanitizer.exception(send_exc), original)
            # Terminal fallback: no recursive report, no remote retries.
