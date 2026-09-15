"""Opt-in task drafts. All owner mutations share the reminder MySQL lock."""
from __future__ import annotations

import re
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, ReplyParameters
from sqlalchemy import select

from app.models import ReminderEditSession, VoiceReminder, VoiceTaskDraft, VoiceTaskDraftSession
from app.openai_service import MONTHS, YouGileTaskExtraction
from app.reminder_service import FORGOTTEN, utcnow
from app.task_service import MOSCOW_TZ

MISSING_DEADLINE = "Не услышал дедлайн. Когда поставить? Напиши в формате 19.01.2004, иначе я тебя не пойму"
INVALID_DATE = "Не тот формат :( Напиши еще раз в формате 19.01.2004"
EDIT_PROMPT = "Напиши следующим сообщением правильную формулировку задачи вместе с дедлайном."
TIMEOUT = "Время вышло, задачу не создал"
STALE = "Этот черновик или действие уже неактуально."
YOUGILE_FAILURE = "Не удалось создать задачу в YouGile. Попробуй нажать «В YouGile» ещё раз."
EDIT_FAILURE = "Не получилось обработать текст. Попробуй отправить его ещё раз."


def human_task(draft, *, today=None):
    today = today or datetime.now(MOSCOW_TZ).date()
    deadline = draft.deadline_date
    if deadline is None:
        raise ValueError("Draft has no deadline")
    value = f"{deadline.day} {MONTHS[deadline.month - 1]}"
    if deadline.year != today.year:
        value += f" {deadline.year}"
    return f"{draft.title} до {value}"


def confirmation_keyboard(draft_id):
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=label, callback_data=f"vd:{action}:{draft_id}")
    ] for label, action in [("✅ В YouGile", "yg"), ("📝 Напомнить тут", "local"),
                            ("✍️ Изменить", "edit"), ("❌ Удалить", "delete")]])


def parse_deadline(value):
    if not re.fullmatch(r"\d{2}\.\d{2}\.\d{4}", value, flags=re.ASCII):
        raise ValueError("Invalid date format")
    return datetime.strptime(value, "%d.%m.%Y").date()


class VoiceTaskDraftService:
    def __init__(self, reminders, identity, yougile):
        self.reminders = reminders
        self.session_factory = reminders.session_factory
        self.bot = reminders.bot
        self.openai = reminders.openai
        self.identity = identity
        self.yougile = yougile
        self.transaction = reminders.transaction

    async def eligible(self, user):
        return await self.destination(user) is not None

    async def destination(self, user):
        return await self.identity.voice_task_project(user.id, getattr(user, "username", None))

    async def report(self, exc, operation, user_id):
        await self.reminders.errors.report(exc, component="voice_task_drafts",
                                          operation=operation, telegram_user_id=user_id)

    async def owned(self, session, user_id, draft_id):
        return await session.scalar(select(VoiceTaskDraft).where(
            VoiceTaskDraft.id == draft_id, VoiceTaskDraft.telegram_user_id == user_id
        ).with_for_update())

    async def reply(self, draft, text, **kwargs):
        return await self.bot.send_message(
            chat_id=draft.original_voice_chat_id, text=text, parse_mode=None,
            reply_parameters=ReplyParameters(message_id=draft.original_voice_message_id,
                                            allow_sending_without_reply=True), **kwargs)

    async def confirm(self, draft):
        return await self.reply(draft, f"Я понял так:\n\n«{human_task(draft)}»",
                                reply_markup=confirmation_keyboard(draft.id))

    async def close_input(self, session, user_id, *, preserve_draft_id=None):
        """Called under the shared owner lock, including from local reminder Edit.

        A replaced complete draft gets its decision UI back. Unreachable incomplete
        drafts are physically deleted. Other users' rows are never touched.
        """
        old = await session.get(VoiceTaskDraftSession, user_id)
        if old is None:
            return
        draft = await self.owned(session, user_id, old.draft_id)
        await self.reminders.remove_keyboard(old.prompt_chat_id, old.prompt_message_id)
        await session.delete(old)
        await session.flush()
        if draft and draft.id != preserve_draft_id:
            if draft.deadline_date is None:
                await session.delete(draft)
            else:
                await self.confirm(draft)
        await session.flush()

    async def begin_input(self, session, draft, mode):
        await self.close_input(session, draft.telegram_user_id, preserve_draft_id=draft.id)
        # Share the one-text-input invariant with the owner's existing local reminders.
        old = await session.get(ReminderEditSession, draft.telegram_user_id)
        if old:
            await self.reminders.remove_keyboard(old.prompt_chat_id, old.prompt_message_id)
            await session.delete(old)
            await session.flush()
        prompt = await self.reply(draft, MISSING_DEADLINE if mode == "deadline" else EDIT_PROMPT)
        now = utcnow()
        session.add(VoiceTaskDraftSession(
            telegram_user_id=draft.telegram_user_id, draft_id=draft.id, mode=mode,
            prompt_chat_id=draft.original_voice_chat_id, prompt_message_id=prompt.message_id,
            expires_at=now + timedelta(minutes=15), created_at=now,
        ))

    async def create_from_voice(self, message):
        if message.chat.type != "private" or not await self.eligible(message.from_user):
            return None
        path = None
        try:
            with tempfile.NamedTemporaryFile(prefix="voice-task-", suffix=".ogg", delete=False) as audio:
                path = Path(audio.name)
            await self.bot.download(message.voice, destination=path)
            transcript = await self.openai.transcribe_voice(path)
            extracted = await self.openai.extract_yougile_task(transcript)
            extracted = YouGileTaskExtraction.model_validate(extracted.model_dump())
            async with self.transaction(message.from_user.id) as session:
                now = utcnow()
                draft = VoiceTaskDraft(
                    telegram_user_id=message.from_user.id, original_voice_chat_id=message.chat.id,
                    original_voice_message_id=message.message_id, transcript=transcript,
                    title=extracted.title, deadline_date=extracted.deadline,
                    created_at=now, updated_at=now,
                )
                session.add(draft)
                await session.flush()
                if draft.deadline_date is None:
                    await self.begin_input(session, draft, "deadline")
                else:
                    await self.confirm(draft)
            return draft
        finally:
            if path is not None:
                try:
                    path.unlink(missing_ok=True)
                except Exception as exc:
                    await self.report(exc, "cleanup_audio", message.from_user.id)

    async def expire_locked(self, session, edit):
        if edit is None or edit.expires_at > utcnow():
            return None
        draft = await self.owned(session, edit.telegram_user_id, edit.draft_id)
        await session.delete(edit)
        await session.flush()
        if draft:
            if edit.mode == "deadline" or draft.deadline_date is None:
                await session.delete(draft)
            # Complete edit timeout retains the prior interpretation and restores UI.
        return draft

    async def timeout_ui(self, edit, draft):
        await self.reminders.remove_keyboard(edit.prompt_chat_id, edit.prompt_message_id)
        await self.bot.send_message(chat_id=edit.prompt_chat_id, text=TIMEOUT)
        if draft and edit.mode == "edit" and draft.deadline_date is not None:
            await self.confirm(draft)

    async def callback(self, callback):
        user_id = callback.from_user.id
        try:
            return await self._callback(callback)
        except Exception as exc:
            await self.report(exc, "draft_callback", user_id)
            await callback.answer(YOUGILE_FAILURE if callback.data.startswith("vd:yg:")
                                  else "Не удалось выполнить действие. Попробуй ещё раз.")

    async def _callback(self, callback):
        try:
            prefix, action, raw_id = callback.data.split(":")
            draft_id = int(raw_id)
        except (ValueError, AttributeError):
            await callback.answer(STALE)
            return
        msg = callback.message
        user_id = callback.from_user.id
        if (prefix != "vd" or action not in {"yg", "local", "edit", "delete"}
                or msg is None or msg.chat.type != "private" or msg.chat.id != user_id):
            await callback.answer(STALE)
            return
        project_title = await self.destination(callback.from_user)
        if project_title is None:
            await callback.answer(STALE)
            return
        expired = None
        result = None
        async with self.transaction(user_id) as session:
            draft = await self.owned(session, user_id, draft_id)
            edit = await session.get(VoiceTaskDraftSession, user_id)
            if edit and edit.draft_id == draft_id and edit.expires_at <= utcnow():
                expired = (edit, await self.expire_locked(session, edit))
            elif draft is None:
                result = "stale"
            elif action in {"yg", "local"} and (
                    draft.deadline_date is None or (edit and edit.draft_id == draft_id)):
                result = "stale"
            elif action == "edit":
                await self.begin_input(session, draft, "edit")
                result = "edit"
            else:
                if action == "yg":
                    # Keep owner/row locks until successful POST and DB commit.
                    # Persistent random key survives rollback, restart and lost HTTP response.
                    await self.yougile.create_voice_task(project_title=project_title, title=draft.title,
                        deadline=draft.deadline_date, idempotency_key=draft.idempotency_key)
                elif action == "local":
                    now = utcnow()
                    session.add(VoiceReminder(
                        telegram_user_id=user_id, original_voice_chat_id=draft.original_voice_chat_id,
                        original_voice_message_id=draft.original_voice_message_id,
                        transcript=draft.transcript, text=human_task(draft),
                        created_at=now, updated_at=now,
                    ))
                    await session.flush()
                await session.delete(draft)  # FK cascade removes its session and personal data.
                result = action
        # Telegram errors after commit cannot roll back a successful external creation/deletion.
        try:
            await self.reminders.remove_keyboard(msg.chat.id, msg.message_id)
            if expired:
                await self.timeout_ui(*expired)
                await callback.answer(STALE)
            elif result == "delete":
                await self.reply(draft, FORGOTTEN)
                await callback.answer()
            else:
                await callback.answer({"yg": "Готово, задача создана в YouGile",
                                       "local": "Готово, буду напоминать здесь",
                                       "stale": STALE, "edit": ""}[result])
        except Exception as exc:
            await self.report(exc, "draft_callback_ui", user_id)

    async def replace_text(self, message):
        if (message.chat.type != "private" or not message.text
                or message.text.lstrip().startswith("/") or not await self.eligible(message.from_user)):
            return False
        user_id = message.from_user.id
        expired = None
        try:
            async with self.transaction(user_id) as session:
                edit = await session.get(VoiceTaskDraftSession, user_id)
                if edit is None:
                    return False
                draft = await self.owned(session, user_id, edit.draft_id)
                if edit.expires_at <= utcnow():
                    expired = (edit, await self.expire_locked(session, edit))
                elif draft is None:
                    await session.delete(edit)
                elif edit.mode == "deadline":
                    try:
                        deadline = parse_deadline(message.text)
                    except ValueError:
                        await message.answer(INVALID_DATE)
                        return True
                    draft.deadline_date = deadline
                    draft.updated_at = utcnow()
                    await session.delete(edit)
                    await self.confirm(draft)
                else:
                    # Do not mutate the previous understood result until extraction succeeds.
                    extracted = await self.openai.extract_yougile_task(message.text)
                    extracted = YouGileTaskExtraction.model_validate(extracted.model_dump())
                    draft.title, draft.deadline_date = extracted.title, extracted.deadline
                    draft.updated_at = utcnow()
                    await session.delete(edit)
                    await session.flush()
                    if draft.deadline_date is None:
                        await self.begin_input(session, draft, "deadline")
                    else:
                        await self.confirm(draft)
            if expired:
                await self.timeout_ui(*expired)
            return True
        except Exception as exc:
            await self.report(exc, "draft_input", user_id)
            await message.answer(EDIT_FAILURE)
            return True

    async def cleanup_expired(self):
        try:
            async with self.session_factory() as session:
                owners = list(await session.scalars(select(VoiceTaskDraftSession.telegram_user_id).where(
                    VoiceTaskDraftSession.expires_at <= utcnow())))
            for user_id in owners:
                try:
                    expired = None
                    async with self.transaction(user_id) as session:
                        edit = await session.get(VoiceTaskDraftSession, user_id)
                        if edit and edit.expires_at <= utcnow():
                            expired = (edit, await self.expire_locked(session, edit))
                    if expired:
                        await self.timeout_ui(*expired)
                except Exception as exc:
                    await self.report(exc, "expire_draft_input", user_id)
        except Exception as exc:
            await self.report(exc, "list_expired_draft_inputs", 0)
