from __future__ import annotations

import asyncio
import logging
import tempfile
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from weakref import WeakValueDictionary

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, ReplyParameters
from sqlalchemy import select, text as sql_text

from app.models import VoiceReminder, ReminderDelivery, ReminderEditSession
from app.openai_service import MAX_REMINDER_LENGTH, ReminderExtraction

logger = logging.getLogger(__name__)
FORGOTTEN = "Окей, забыл об этом"
DONE = "Хорошо, перестану тебе напоминать об этой задаче"
EDIT_PROMPT = "Напиши следующим сообщением правильную формулировку напоминания."
TIMEOUT = "Время на редактирование вышло, поэтому сохранил напоминание таким, каким оно и было"
VOICE_FAILURE = "Не получилось разобрать голосовое. Попробуй отправить его ещё раз."
STALE = "Это напоминание или действие уже неактуально."
# Local lock shared across service instances, including SQLite tests. MySQL additionally
# uses a connection-scoped advisory lock; no personal lock records survive deletion.
_LOCKS: WeakValueDictionary = WeakValueDictionary()


def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def confirmation_keyboard(reminder_id):
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=label, callback_data=f"vr:{action}:{reminder_id}")
        for label, action in [("✅ Ок", "ok"), ("✍️ Изменить", "edit"), ("❌ Удалить", "delete")]
    ]])


def edit_keyboard(reminder_id):
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=label, callback_data=f"vr:{action}:{reminder_id}")
    ] for label, action in [("✅ Оставить, как было", "keep"),
                            ("❌ Отменить напоминание и удалить его", "cancel")]])


class ReminderService:
    def __init__(self, session_factory, bot, openai_service, error_reporter):
        self.session_factory = session_factory
        self.bot = bot
        self.openai = openai_service
        self.errors = error_reporter

    @asynccontextmanager
    async def transaction(self, user_id):
        """Serialize ALL mutations/send lookups for one owner, across reminders.

        MySQL GET_LOCK stays on the same checked-out connection until COMMIT and
        release. This also serializes first edit-session creation (no row yet).
        Telegram send and ledger commit have the same practical crash window as
        the existing digest sender: Telegram itself cannot join a DB transaction.
        """
        key = (id(self.session_factory.kw.get("bind")), user_id)
        lock = _LOCKS.setdefault(key, asyncio.Lock())
        async with lock, self.session_factory() as session:
            async with session.bind.connect() as connection:
                mysql = connection.dialect.name == "mysql"
                name = f"voice-reminder:{user_id}"
                acquired = False
                try:
                    if mysql:
                        acquired = await connection.scalar(
                            sql_text("SELECT GET_LOCK(:name, 30)"), {"name": name}) == 1
                        await connection.commit()
                        if not acquired:
                            raise TimeoutError("Reminder owner lock timed out")
                    # Bind this session to the checked-out connection holding GET_LOCK.
                    session.bind = connection
                    async with session.begin():
                        yield session
                finally:
                    if acquired:
                        try:
                            await connection.execute(sql_text("SELECT RELEASE_LOCK(:name)"), {"name": name})
                            await connection.commit()
                        except BaseException:
                            # Never return a connection holding a named lock to the pool.
                            await connection.invalidate()
                            raise

    async def report(self, exc, operation, **ids):
        await self.errors.report(exc, component="voice_reminders", operation=operation, **ids)

    async def remove_keyboard(self, chat_id, message_id):
        try:
            await self.bot.edit_message_reply_markup(chat_id=chat_id, message_id=message_id,
                                                     reply_markup=None)
        except TelegramBadRequest as exc:
            # Duplicate taps and already-deleted prompts are expected Telegram state.
            if any(part in exc.message.lower() for part in (
                "message is not modified", "message to edit not found", "message can't be edited"
            )):
                return
            await self.report(exc, "remove_keyboard", telegram_chat_id=chat_id)
        except Exception as exc:
            await self.report(exc, "remove_keyboard", telegram_chat_id=chat_id)

    async def create_from_voice(self, message):
        path = None
        try:
            with tempfile.NamedTemporaryFile(prefix="voice-reminder-", suffix=".ogg", delete=False) as audio:
                path = Path(audio.name)
            await self.bot.download(message.voice, destination=path)
            transcript = await self.openai.transcribe_voice(path)
            reminder_text = await self.openai.extract_reminder(transcript)
            reminder_text = ReminderExtraction(reminder=reminder_text).reminder
            async with self.transaction(message.from_user.id) as session:
                now = utcnow()
                reminder = VoiceReminder(
                    telegram_user_id=message.from_user.id,
                    original_voice_chat_id=message.chat.id,
                    original_voice_message_id=message.message_id,
                    transcript=transcript, text=reminder_text, created_at=now, updated_at=now,
                )
                session.add(reminder)
                await session.flush()
            # Already committed and active before presenting the optional review UI.
            await self.bot.send_message(
                chat_id=message.chat.id, text=f"Я понял так:\n\n«{reminder.text}»",
                parse_mode=None, reply_parameters=ReplyParameters(
                    message_id=message.message_id, allow_sending_without_reply=True),
                reply_markup=confirmation_keyboard(reminder.id),
            )
            return reminder
        finally:
            if path is not None:
                try:
                    path.unlink(missing_ok=True)
                except Exception as exc:
                    await self.report(exc, "cleanup_audio", telegram_user_id=message.from_user.id)

    async def _owned(self, session, user_id, reminder_id):
        return await session.scalar(select(VoiceReminder).where(
            VoiceReminder.id == reminder_id, VoiceReminder.telegram_user_id == user_id
        ).with_for_update())

    async def _expire(self, session, edit):
        if edit is None or edit.expires_at > utcnow():
            return False
        await session.delete(edit)
        await session.flush()
        return True

    async def _timeout_ui(self, edit):
        await self.remove_keyboard(edit.prompt_chat_id, edit.prompt_message_id)
        await self.bot.send_message(chat_id=edit.prompt_chat_id, text=TIMEOUT)

    async def callback(self, callback):
        try:
            prefix, action, raw_id = callback.data.split(":")
            reminder_id = int(raw_id)
        except (ValueError, AttributeError):
            await callback.answer(STALE)
            return
        if prefix != "vr" or action not in {"ok", "edit", "delete", "keep", "cancel"}:
            await callback.answer(STALE)
            return
        user_id = callback.from_user.id
        message = callback.message
        if message is None or message.chat.type != "private" or message.chat.id != user_id:
            await callback.answer(STALE)
            return
        expired = None
        old_edit = None
        deleted = None
        new_prompt = None
        try:
            async with self.transaction(user_id) as session:
                reminder = await self._owned(session, user_id, reminder_id)
                if reminder is None:
                    await callback.answer(STALE)
                    await self.remove_keyboard(message.chat.id, message.message_id)
                    return
                if action == "ok":
                    await callback.answer()
                    await self.remove_keyboard(message.chat.id, message.message_id)
                    return
                edit = await session.get(ReminderEditSession, user_id)
                if action != "delete" and await self._expire(session, edit):
                    expired, edit = edit, None
                if action in {"keep", "cancel"} and (
                    edit is None or edit.reminder_id != reminder_id or
                    (edit.prompt_chat_id, edit.prompt_message_id) != (message.chat.id, message.message_id)
                ):
                    await callback.answer(STALE)
                    await self.remove_keyboard(message.chat.id, message.message_id)
                else:
                    await callback.answer()
                    await self.remove_keyboard(message.chat.id, message.message_id)
                    if action in {"delete", "cancel"}:
                        deleted = (reminder.original_voice_chat_id, reminder.original_voice_message_id)
                        old_edit = edit if edit and edit.reminder_id == reminder_id else None
                        await session.delete(reminder)
                    elif action == "edit":
                        if edit:
                            old_edit = edit
                            await session.delete(edit)
                            await session.flush()
                            await self.remove_keyboard(edit.prompt_chat_id, edit.prompt_message_id)
                        new_prompt = await self.bot.send_message(chat_id=user_id, text=EDIT_PROMPT,
                                                                reply_markup=edit_keyboard(reminder_id))
                        now = utcnow()
                        session.add(ReminderEditSession(
                            telegram_user_id=user_id, reminder_id=reminder_id,
                            prompt_chat_id=user_id, prompt_message_id=new_prompt.message_id,
                            expires_at=now + timedelta(minutes=15), created_at=now,
                        ))
                    elif action == "keep":
                        await session.delete(edit)
        except BaseException:
            if new_prompt is not None:
                await self.remove_keyboard(user_id, new_prompt.message_id)
            raise
        if expired:
            await self._timeout_ui(expired)
        if old_edit:
            await self.remove_keyboard(old_edit.prompt_chat_id, old_edit.prompt_message_id)
        if deleted:
            await self.bot.send_message(chat_id=deleted[0], text=FORGOTTEN,
                                       reply_parameters=ReplyParameters(message_id=deleted[1],
                                                                       allow_sending_without_reply=True))

    async def replace_text(self, message):
        value = (message.text or "").strip()
        if value.startswith("/") or message.chat.type != "private":
            return False
        expired = None
        edit = None
        async with self.transaction(message.from_user.id) as session:
            edit = await session.get(ReminderEditSession, message.from_user.id)
            if edit is None:
                return False
            if await self._expire(session, edit):
                expired = edit
            else:
                if not value or len(value) > MAX_REMINDER_LENGTH:
                    await message.answer(f"Напиши непустой текст длиной до {MAX_REMINDER_LENGTH} символов.")
                    return True
                reminder = await self._owned(session, message.from_user.id, edit.reminder_id)
                if reminder is None:
                    await session.delete(edit)
                    return False
                reminder.text = value
                reminder.updated_at = utcnow()
                await session.delete(edit)
        if expired:
            await self._timeout_ui(expired)
        else:
            await self.remove_keyboard(edit.prompt_chat_id, edit.prompt_message_id)
            await message.answer(f"Готово, теперь буду напоминать так:\n\n«{value}»", parse_mode=None)
        return True

    async def done(self, message):
        replied = message.reply_to_message
        if replied is None:
            await message.answer("Отправь /done ответом на сообщение с напоминанием.")
            return
        old_edit = None
        async with self.transaction(message.from_user.id) as session:
            delivery = await session.scalar(select(ReminderDelivery).where(
                ReminderDelivery.telegram_chat_id == message.chat.id,
                ReminderDelivery.telegram_message_id == replied.message_id,
            ))
            reminder = await self._owned(session, message.from_user.id, delivery.reminder_id) if delivery else None
            if reminder is None:
                await message.answer("Это не активное напоминание. Ответь /done на сообщение с напоминанием.")
                return
            old_edit = await session.get(ReminderEditSession, message.from_user.id)
            if old_edit and old_edit.reminder_id != reminder.id:
                old_edit = None
            await session.delete(reminder)
        if old_edit:
            await self.remove_keyboard(old_edit.prompt_chat_id, old_edit.prompt_message_id)
        await self.bot.send_message(chat_id=message.chat.id, text=DONE,
                                   reply_parameters=ReplyParameters(message_id=replied.message_id,
                                                                   allow_sending_without_reply=True))

    async def deliver(self, user_id, *, scheduled_date=None):
        async with self.session_factory() as session:
            ids = list(await session.scalars(select(VoiceReminder.id).where(
                VoiceReminder.telegram_user_id == user_id
            ).order_by(VoiceReminder.created_at, VoiceReminder.id)))
        for reminder_id in ids:
            try:
                async with self.transaction(user_id) as session:
                    reminder = await self._owned(session, user_id, reminder_id)
                    if reminder is None:
                        continue
                    if scheduled_date is not None and await session.scalar(select(ReminderDelivery.id).where(
                        ReminderDelivery.reminder_id == reminder_id,
                        ReminderDelivery.scheduled_date == scheduled_date,
                    )) is not None:
                        continue
                    delivered = await self.bot.send_message(chat_id=user_id, text=f"📝 {reminder.text}", parse_mode=None)
                    session.add(ReminderDelivery(
                        reminder_id=reminder_id, telegram_chat_id=user_id,
                        telegram_message_id=delivered.message_id, sent_at=utcnow(),
                        scheduled_date=scheduled_date,
                    ))
            except Exception as exc:
                await self.report(exc, "deliver", telegram_user_id=user_id, reminder_id=reminder_id)

    async def deliver_remaining(self, dispatch_date, *, exclude=()):
        try:
            async with self.session_factory() as session:
                owners = list(await session.scalars(select(VoiceReminder.telegram_user_id).distinct().order_by(
                    VoiceReminder.telegram_user_id)))
            for user_id in owners:
                if user_id in exclude:
                    continue
                try:
                    await self.deliver(user_id, scheduled_date=dispatch_date)
                except Exception as exc:
                    await self.report(exc, "scheduled_delivery", telegram_user_id=user_id)
        except Exception as exc:
            await self.report(exc, "list_reminder_owners")

    async def cleanup_expired(self):
        try:
            async with self.session_factory() as session:
                owners = list(await session.scalars(select(ReminderEditSession.telegram_user_id).where(
                    ReminderEditSession.expires_at <= utcnow())))
            for user_id in owners:
                try:
                    expired = None
                    async with self.transaction(user_id) as session:
                        edit = await session.get(ReminderEditSession, user_id)
                        if await self._expire(session, edit):
                            expired = edit
                    if expired:
                        await self._timeout_ui(expired)
                except Exception as exc:
                    await self.report(exc, "expire_edit", telegram_user_id=user_id)
        except Exception as exc:
            await self.report(exc, "list_expired_edits")
