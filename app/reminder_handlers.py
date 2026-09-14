from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery
from aiogram.utils.chat_action import ChatActionSender

from app.openai_service import UnusableVoice
from app.personal_identity import UNKNOWN
from app.reminder_service import VOICE_FAILURE


def create_reminder_router(service, identity):
    router = Router(name="voice_reminders")

    async def known(message):
        if message.from_user is None or identity is None:
            await message.answer(UNKNOWN)
            return False
        uid, _ = await identity.resolve(message.from_user.id, message.from_user.username)
        if uid is None:
            await message.answer(UNKNOWN)
            return False
        return True

    @router.message(F.chat.type == "private", Command("done"))
    async def done(message: Message):
        if await known(message):
            await service.done(message)

    @router.message(F.chat.type == "private", F.voice)
    async def voice(message: Message):
        # Send immediately (ChatActionSender's background task alone is not immediate).
        await service.bot.send_chat_action(chat_id=message.chat.id, action="typing")
        async with ChatActionSender.typing(chat_id=message.chat.id, bot=service.bot, initial_sleep=4):
            if not await known(message):
                return
            try:
                if service.task_drafts is not None and await service.task_drafts.eligible(message.from_user):
                    await service.task_drafts.create_from_voice(message)
                else:
                    await service.create_from_voice(message)
            except UnusableVoice:
                await message.answer(VOICE_FAILURE)
            except Exception as exc:
                await service.report(exc, "process_voice", telegram_user_id=message.from_user.id)
                await message.answer(VOICE_FAILURE)

    @router.callback_query(F.data.startswith("vr:"))
    async def reminder_callback(callback: CallbackQuery):
        await service.callback(callback)

    @router.callback_query(F.data.startswith("vd:"))
    async def draft_callback(callback: CallbackQuery):
        if service.task_drafts is not None:
            await service.task_drafts.callback(callback)
        else:
            await callback.answer("Этот черновик или действие уже неактуально.")

    @router.message(F.chat.type == "private", F.text, ~F.text.lstrip().startswith("/"))
    async def replacement_text(message: Message):
        if message.from_user:
            if service.task_drafts is not None and await service.task_drafts.replace_text(message):
                return
            await service.replace_text(message)

    return router
