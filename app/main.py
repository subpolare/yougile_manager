from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from app.config import Settings, load_employee_mappings
from app.db import create_engine_and_session, initialize_database
from app.scheduler import create_scheduler
from app.task_service import DigestService
from app.telegram_handlers import create_router
from app.yougile import YouGileClient


logger = logging.getLogger(__name__)


async def run() -> None:
    settings = Settings()
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    logger.info("Bot startup started company_id=%s", settings.yougile_company_id)

    engine, session_factory = create_engine_and_session(settings.sqlalchemy_url)
    await initialize_database(
        engine,
        attempts=settings.db_connect_attempts,
        retry_seconds=settings.db_connect_retry_seconds,
    )

    bot = Bot(
        token=settings.telegram_bot_token.get_secret_value(),
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    yougile = YouGileClient(
        settings.yougile_api_key.get_secret_value(),
        base_url=settings.yougile_base_url,
    )
    digest_service = DigestService(yougile, load_employee_mappings())
    dispatcher = Dispatcher()
    dispatcher.include_router(
        create_router(
            session_factory=session_factory,
            yougile=yougile,
            digest_service=digest_service,
        )
    )
    scheduler = create_scheduler(
        bot=bot,
        session_factory=session_factory,
        yougile=yougile,
        digest_service=digest_service,
    )

    try:
        await bot.delete_webhook(drop_pending_updates=False)
        scheduler.start()
        logger.info("Bot polling started; daily schedule is 12:00 Europe/Moscow")
        await dispatcher.start_polling(bot)
    finally:
        if scheduler.running:
            scheduler.shutdown(wait=False)
        await yougile.aclose()
        await bot.session.close()
        await engine.dispose()
        logger.info("Bot stopped")


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()

