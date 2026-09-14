from __future__ import annotations

import logging
from datetime import datetime

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy.exc import SQLAlchemyError

from app.db import (SessionFactory, dispatch_once, list_bindings,
                    list_personal_subscriptions, dispatch_personal_once)
from app.personal_digest import PersonalDigestService, personal_task_index
from app.task_service import DigestService, MOSCOW_TZ
from app.yougile import YouGileClient, YouGileError


logger = logging.getLogger(__name__)


async def run_daily_dispatch(
    *,
    bot: Bot,
    session_factory: SessionFactory,
    yougile: YouGileClient,
    digest_service: DigestService,
    personal_service: PersonalDigestService | None = None,
) -> None:
    dispatch_date = datetime.now(MOSCOW_TZ).date()
    if dispatch_date.weekday() >= 5:
        return
    logger.info("Automatic daily dispatch started date=%s", dispatch_date)
    try:
        bindings = await list_bindings(session_factory)
        subscriptions = await list_personal_subscriptions(session_factory)
    except SQLAlchemyError as exc:
        logger.error("Could not load bindings error=%s", type(exc).__name__)
        return
    if not bindings and not subscriptions:
        logger.info("Automatic daily dispatch has no destinations")
        return

    try:
        snapshot = await yougile.fetch_workspace()
    except YouGileError:
        logger.exception("YouGile workspace fetch failed during automatic dispatch")
        return

    for binding in bindings:
        try:
            chunks, buckets = await digest_service.build(
                binding.yougile_project_id,
                snapshot=snapshot,
                today=dispatch_date,
            )
        except Exception as exc:
            logger.exception(
                "Automatic digest generation failed chat_id=%s project_id=%s error=%s",
                binding.telegram_chat_id,
                binding.yougile_project_id,
                type(exc).__name__,
            )
            continue
        logger.info(
            "Automatic digest ready project_id=%s chat_id=%s today=%s week=%s overdue=%s",
            binding.yougile_project_id,
            binding.telegram_chat_id,
            len(buckets.today),
            len(buckets.week),
            len(buckets.overdue),
        )

        async def send_all() -> None:
            for chunk in chunks:
                await bot.send_message(chat_id=binding.telegram_chat_id, text=chunk)

        try:
            sent = await dispatch_once(
                session_factory,
                chat_id=binding.telegram_chat_id,
                expected_project_id=binding.yougile_project_id,
                dispatch_date=dispatch_date,
                sender=send_all,
            )
        except TelegramAPIError as exc:
            logger.error(
                "Telegram automatic send failed chat_id=%s error=%s",
                binding.telegram_chat_id,
                type(exc).__name__,
            )
        except SQLAlchemyError as exc:
            logger.error(
                "Automatic dispatch database failure chat_id=%s error=%s",
                binding.telegram_chat_id,
                type(exc).__name__,
            )
        except Exception:
            logger.exception("Automatic send failed chat_id=%s", binding.telegram_chat_id)
        else:
            logger.info(
                "Automatic dispatch result chat_id=%s sent=%s",
                binding.telegram_chat_id,
                sent,
            )
    if personal_service is not None and subscriptions:
        index = personal_task_index(snapshot)
        for subscription in subscriptions:
            try:
                chunks, _ = await personal_service.build(
                    subscription.yougile_user_id, snapshot=snapshot,
                    today=dispatch_date, index=index,
                )

                async def send_personal() -> None:
                    for chunk in chunks:
                        await bot.send_message(chat_id=subscription.telegram_user_id, text=chunk)

                sent = await dispatch_personal_once(
                    session_factory, telegram_id=subscription.telegram_user_id,
                    expected_user_id=subscription.yougile_user_id,
                    dispatch_date=dispatch_date, sender=send_personal,
                )
                logger.info("Personal automatic dispatch user_id=%s sent=%s",
                            subscription.telegram_user_id, sent)
            except Exception:
                logger.exception("Personal automatic dispatch failed user_id=%s",
                                 subscription.telegram_user_id)
    logger.info("Automatic daily dispatch finished date=%s", dispatch_date)


def create_scheduler(
    *,
    bot: Bot,
    session_factory: SessionFactory,
    yougile: YouGileClient,
    digest_service: DigestService,
    personal_service: PersonalDigestService | None = None,
) -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(timezone=MOSCOW_TZ)
    scheduler.add_job(
        run_daily_dispatch,
        trigger=CronTrigger(day_of_week="mon-fri", hour=12, minute=0, timezone=MOSCOW_TZ),
        kwargs={
            "bot": bot,
            "session_factory": session_factory,
            "yougile": yougile,
            "digest_service": digest_service,
            "personal_service": personal_service,
        },
        id="daily-yougile-digest",
        replace_existing=True,
        coalesce=True,
        max_instances=1,
        misfire_grace_time=60,
    )
    return scheduler
