from __future__ import annotations

import html
import logging

from aiogram import Bot, Router
from aiogram.enums import ChatMemberStatus, ChatType
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command
from aiogram.filters.command import CommandObject
from aiogram.types import Message
from aiogram.utils.chat_action import ChatActionSender
from sqlalchemy.exc import SQLAlchemyError

from app.db import SessionFactory, get_binding, replace_binding
from app.task_service import DigestService, match_projects, project_number
from app.yougile import YouGileClient, YouGileError


logger = logging.getLogger(__name__)


def create_router(
    *,
    session_factory: SessionFactory,
    yougile: YouGileClient,
    digest_service: DigestService,
) -> Router:
    router = Router(name="commands")

    @router.message(Command("init"))
    async def initialize(message: Message, command: CommandObject, bot: Bot) -> None:
        if not _is_group(message):
            await message.answer("Эта команда работает только в групповых чатах.")
            return
        if not await _is_admin(message, bot):
            await message.answer("Только администратор или владелец чата может использовать /init.")
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
            except YouGileError:
                logger.exception(
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
            logger.error(
                "Binding read failed chat_id=%s error=%s", message.chat.id, type(exc).__name__
            )
            await message.answer("Не удалось прочитать привязку. Попробуйте /status позже.")
            return
        if binding is None:
            await message.answer(
                "Этот чат пока не связан с проектом. Попросите администратора использовать /init."
            )
            return
        await message.answer(
            "Этот чат связан с:\n"
            f"{html.escape(binding.project_title)}\n"
            f"YouGile project ID: {html.escape(binding.yougile_project_id)}\n"
            "Ежедневная рассылка: 12:00 МСК"
        )

    @router.message(Command("task"))
    async def task(message: Message, bot: Bot) -> None:
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
                logger.error(
                    "Binding read failed chat_id=%s error=%s",
                    message.chat.id,
                    type(exc).__name__,
                )
                await message.answer("Не удалось прочитать привязку. Попробуйте /task позже.")
                return
            if binding is None:
                await message.answer(
                    "Этот чат не инициализирован. Попросите администратора использовать /init."
                )
                return

            logger.info(
                "Manual digest requested chat_id=%s project_id=%s",
                message.chat.id,
                binding.yougile_project_id,
            )
            try:
                chunks, buckets = await digest_service.build(binding.yougile_project_id)
            except YouGileError:
                logger.exception(
                    "YouGile task lookup failed for /task chat_id=%s project_id=%s",
                    message.chat.id,
                    binding.yougile_project_id,
                )
                await message.answer(
                    "Не удалось получить задачи из YouGile. Попробуйте /task позже."
                )
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
                logger.error(
                    "Telegram send failed chat_id=%s error=%s",
                    message.chat.id,
                    type(exc).__name__,
                )

    return router


def _is_group(message: Message) -> bool:
    return message.chat.type in {ChatType.GROUP, ChatType.SUPERGROUP}


async def _is_admin(message: Message, bot: Bot) -> bool:
    if message.from_user is None:
        return False
    try:
        member = await bot.get_chat_member(message.chat.id, message.from_user.id)
    except TelegramAPIError as exc:
        logger.warning(
            "Telegram admin check failed chat_id=%s error=%s",
            message.chat.id,
            type(exc).__name__,
        )
        return False
    return member.status in {ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR}
