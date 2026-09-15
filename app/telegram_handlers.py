from __future__ import annotations

import html
import logging

from aiogram import Bot, Router
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command
from aiogram.filters.command import CommandObject
from aiogram.types import Message
from aiogram.utils.chat_action import ChatActionSender
from sqlalchemy.exc import SQLAlchemyError

from app.db import SessionFactory, get_binding, replace_binding
from app.personal_digest import PersonalDigestService
from app.personal_identity import PersonalIdentity, UNKNOWN, START, RETURNING, STOP
from app.task_service import DigestService, match_projects, project_number
from app.yougile import YouGileClient, YouGileError


logger = logging.getLogger(__name__)


def create_router(
    *,
    session_factory: SessionFactory,
    yougile: YouGileClient,
    digest_service: DigestService,
    personal_service: PersonalDigestService | None = None,
    personal_identity: PersonalIdentity | None = None,
    reminder_service=None,
    error_reporter=None,
) -> Router:
    router = Router(name="commands")

    async def report(exc, operation, message):
        if error_reporter is not None:
            await error_reporter.report(exc, component="telegram", operation=operation,
                                        telegram_chat_id=message.chat.id)

    if error_reporter is not None:
        from app.runtime_errors import RuntimeErrorMiddleware
        router.message.outer_middleware(RuntimeErrorMiddleware(error_reporter))
        router.callback_query.outer_middleware(RuntimeErrorMiddleware(error_reporter))

    async def subscription_command(message: Message, action: str) -> None:
        if message.chat.type != ChatType.PRIVATE:
            return
        if message.from_user is None or personal_identity is None:
            await message.answer(UNKNOWN)
            return
        try:
            uid, returning = await personal_identity.resolve(
                message.from_user.id, message.from_user.username, action=action
            )
        except SQLAlchemyError as exc:
            await report(exc, "command", message)
            logger.error("Personal subscription update failed user_id=%s", message.from_user.id)
            await message.answer(f"Не удалось сохранить подписку. Попробуйте /{action} позже.")
            return
        await message.answer(UNKNOWN if uid is None else (
            STOP if action == "stop" else RETURNING if returning else START
        ))

    @router.message(Command("start"))
    async def start(message: Message) -> None:
        await subscription_command(message, "start")

    @router.message(Command("stop"))
    async def stop(message: Message) -> None:
        await subscription_command(message, "stop")

    async def private_task(message: Message, bot: Bot) -> None:
        async with ChatActionSender.typing(chat_id=message.chat.id, bot=bot):
            if message.from_user is None or personal_identity is None or personal_service is None:
                await message.answer(UNKNOWN)
                return
            uid, _ = await personal_identity.resolve(
                message.from_user.id, message.from_user.username
            )
            if uid is None:
                await message.answer(UNKNOWN)
                return
            try:
                chunks, _ = await personal_service.build(uid)
                for chunk in chunks:
                    await message.answer(chunk)
            except Exception as exc:
                await report(exc, "private_task", message)
                failure = ("Не удалось получить задачи из YouGile. Попробуйте /task позже."
                           if isinstance(exc, YouGileError) else
                           "Не удалось подготовить дайджест. Попробуйте /task позже.")
                try:
                    await message.answer(failure)
                except Exception as send_exc:
                    await report(send_exc, "private_task_error_response", message)
            finally:
                if reminder_service is not None:
                    await reminder_service.deliver(message.from_user.id)

    @router.message(Command("init"))
    async def initialize(message: Message, command: CommandObject, bot: Bot) -> None:
        if not _is_group(message):
            await message.answer("Эта команда работает только в групповых чатах.")
            return

        argument = (command.args or "").strip()
        if not argument:
            await message.answer("Укажите номер или точное название проекта: /init 7")
            return
        async with ChatActionSender.typing(
            chat_id=message.chat.id,
            bot=bot,
            message_thread_id=message.message_thread_id,
        ):
            try:
                projects = await yougile.fetch_projects()
            except YouGileError as exc:
                await report(exc, "command", message)
                logger.error(
                    "YouGile project lookup failed for /init chat_id=%s", message.chat.id
                )
                await message.answer("Не удалось получить проекты из YouGile. Попробуйте /init позже.")
                return

            matches = match_projects(projects, argument)
            if len(matches) != 1:
                if argument.isdecimal() and len(matches) > 1:
                    titles = "\n".join(f"• {html.escape(item.title)}" for item in matches)
                    await message.answer(
                        "Найдено несколько проектов с этим номером:\n"
                        f"{titles}\n"
                        "Используйте точное полное название проекта после /init."
                    )
                elif argument.isdecimal():
                    await message.answer(
                        "Проект с таким номером не найден или недоступен для привязки."
                    )
                else:
                    await message.answer(
                        "Проект с таким точным названием не найден или недоступен для привязки."
                    )
                return

            project = matches[0]
            try:
                await replace_binding(
                    session_factory,
                    chat_id=message.chat.id,
                    project_id=project.id,
                    project_number=project_number(project),
                    project_title=project.title,
                )
            except SQLAlchemyError as exc:
                await report(exc, "command", message)
                logger.error(
                    "Binding update failed chat_id=%s project_id=%s error=%s",
                    message.chat.id,
                    project.id,
                    type(exc).__name__,
                )
                await message.answer("Не удалось сохранить привязку. Попробуйте /init позже.")
                return

            logger.info(
                "Binding changed chat_id=%s project_id=%s project_number=%s",
                message.chat.id,
                project.id,
                project_number(project),
            )
            await message.answer(
                f"Готово! Связал этот чат (ID: {message.chat.id}) с проектом "
                f"{html.escape(project.title)} в YouGile. Теперь буду спамить вам уведомлениями "
                "о задачах, вам (не) понравится"
            )

    @router.message(Command("status"))
    async def status(message: Message) -> None:
        if not _is_group(message):
            await message.answer("Эта команда работает только в групповых чатах.")
            return
        try:
            binding = await get_binding(session_factory, message.chat.id)
        except SQLAlchemyError as exc:
            await report(exc, "command", message)
            logger.error(
                "Binding read failed chat_id=%s error=%s", message.chat.id, type(exc).__name__
            )
            await message.answer("Не удалось прочитать привязку. Попробуйте /status позже.")
            return
        if binding is None:
            await message.answer(
                "Этот чат пока не связан с проектом. Используйте /init для привязки проекта."
            )
            return
        await message.answer(
            "Этот чат связан с:\n"
            f"{html.escape(binding.project_title)}\n"
            f"YouGile project ID: {html.escape(binding.yougile_project_id)}\n"
            "Рассылка по будням: 12:00 МСК"
        )

    @router.message(Command("task"))
    async def task(message: Message, bot: Bot) -> None:
        if message.chat.type == ChatType.PRIVATE:
            await private_task(message, bot)
            return
        if not _is_group(message):
            await message.answer("Эта команда работает только в групповых чатах.")
            return
        async with ChatActionSender.typing(
            chat_id=message.chat.id,
            bot=bot,
            message_thread_id=message.message_thread_id,
        ):
            try:
                binding = await get_binding(session_factory, message.chat.id)
            except SQLAlchemyError as exc:
                await report(exc, "command", message)
                logger.error(
                    "Binding read failed chat_id=%s error=%s",
                    message.chat.id,
                    type(exc).__name__,
                )
                await message.answer("Не удалось прочитать привязку. Попробуйте /task позже.")
                return
            if binding is None:
                await message.answer(
                    "Этот чат не инициализирован. Используйте /init для привязки проекта."
                )
                return

            logger.info(
                "Manual digest requested chat_id=%s project_id=%s",
                message.chat.id,
                binding.yougile_project_id,
            )
            try:
                chunks, buckets = await digest_service.build(binding.yougile_project_id)
            except YouGileError as exc:
                await report(exc, "command", message)
                logger.error(
                    "YouGile task lookup failed for /task chat_id=%s project_id=%s",
                    message.chat.id,
                    binding.yougile_project_id,
                )
                await message.answer(
                    "Не удалось получить задачи из YouGile. Попробуйте /task позже."
                )
                return
            except SQLAlchemyError as exc:
                await report(exc, "command", message)
                logger.error("Group greeting database failure chat_id=%s", message.chat.id)
                await message.answer("Не удалось подготовить дайджест. Попробуйте /task позже.")
                return
            logger.info(
                "Digest ready project_id=%s today=%s week=%s overdue=%s",
                binding.yougile_project_id,
                len(buckets.today),
                len(buckets.week),
                len(buckets.overdue),
            )
            try:
                for chunk in chunks:
                    await message.answer(chunk)
            except TelegramAPIError as exc:
                await report(exc, "command", message)
                logger.error(
                    "Telegram send failed chat_id=%s error=%s",
                    message.chat.id,
                    type(exc).__name__,
                )

    if reminder_service is not None:
        from app.reminder_handlers import create_reminder_router
        router.include_router(create_reminder_router(reminder_service, personal_identity))
    return router


def _is_group(message: Message) -> bool:
    return message.chat.type in {ChatType.GROUP, ChatType.SUPERGROUP}
