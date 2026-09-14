from __future__ import annotations

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message


class RuntimeErrorMiddleware(BaseMiddleware):
    def __init__(self, reporter):
        self.reporter = reporter

    async def __call__(self, handler, event, data):
        try:
            if isinstance(event, Message) and event.chat.type == "private":
                try:
                    await self.reporter.observe_admin(event.from_user)
                except Exception as exc:
                    await self.reporter.report(exc, component="telegram", operation="observe_admin")
            return await handler(event, data)
        except Exception as exc:
            await self.reporter.report(exc, component="telegram", operation="handle_update")
            try:
                if isinstance(event, CallbackQuery):
                    await event.answer("Не удалось выполнить действие. Попробуй ещё раз.", show_alert=True)
                elif isinstance(event, Message):
                    await event.answer("Не получилось выполнить действие. Попробуй ещё раз.")
            except Exception as send_exc:
                await self.reporter.report(send_exc, component="telegram", operation="send_error_response")
            return None


class PollingError(RuntimeError):
    """Telegram polling transport failed (details intentionally excluded)."""
