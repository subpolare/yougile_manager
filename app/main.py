from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from app.config import Settings, load_employee_mappings
from app.db import create_engine_and_session, initialize_database
from app.daily_greeting import DailyGreetingProvider
from app.error_reporter import ErrorReporter, PrivacyLogFilter
from app.openai_service import OpenAIService
from app.reminder_service import ReminderService
from app.voice_task_drafts import VoiceTaskDraftService
from app.runtime_errors import PollingError
from app.personal_digest import PersonalDigestService
from app.personal_identity import PersonalIdentity
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
    # Disable SDK wire/debug logging even when application LOG_LEVEL=DEBUG.
    for name in ("openai", "httpx", "httpcore", "sqlalchemy.engine", "aiogram.event"):
        logging.getLogger(name).setLevel(logging.CRITICAL)
    engine, session_factory = create_engine_and_session(settings.sqlalchemy_url)
    bot = Bot(token=settings.telegram_bot_token.get_secret_value(),
              default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    openai_service = OpenAIService(settings)
    secrets = [settings.sqlalchemy_url] + [value.get_secret_value()
               for value in (settings.openai_api_key, settings.telegram_bot_token,
                             settings.yougile_api_key, settings.mysql_password) if value]
    reporter = ErrorReporter(bot, session_factory, openai_service,
                             admin_id=settings.error_admin_telegram_user_id, secrets=secrets)
    for handler in logging.getLogger().handlers:
        handler.addFilter(PrivacyLogFilter(reporter.sanitizer))
    yougile = YouGileClient(settings.yougile_api_key.get_secret_value(),
                            base_url=settings.yougile_base_url)
    scheduler = None
    loop = asyncio.get_running_loop()
    old_exception_handler = loop.get_exception_handler()
    pending_reports = set()

    def background_error(loop, context):
        exc = context.get("exception") or RuntimeError("Background task failed")
        task = loop.create_task(reporter.report(exc, component="runtime", operation="background_task"))
        pending_reports.add(task)
        task.add_done_callback(pending_reports.discard)

    class PollingLogHandler(logging.Handler):
        def emit(self, record):
            if record.levelno >= logging.ERROR:
                # aiogram retries polling internally without raising to run().
                background_error(loop, {"exception": PollingError()})

    polling_logger = logging.getLogger("aiogram.dispatcher")
    polling_handler = PollingLogHandler(level=logging.ERROR)
    polling_logger.addHandler(polling_handler)
    loop.set_exception_handler(background_error)
    try:
        await initialize_database(engine, attempts=settings.db_connect_attempts,
                                  retry_seconds=settings.db_connect_retry_seconds)
        await reporter.startup_check()
        if openai_service.client is None:
            logger.critical("OPENAI_API_KEY is not configured: voice creation unavailable; existing digests and reminder delivery remain enabled")
        mappings = load_employee_mappings()
        greetings = DailyGreetingProvider(session_factory)
        digest_service = DigestService(yougile, mappings, greetings)
        personal_service = PersonalDigestService(yougile, greetings)
        reminders = ReminderService(session_factory, bot, openai_service, reporter)
        identity = PersonalIdentity(session_factory, mappings,
                                    voice_task_employees=settings.voice_task_employees)
        reminders.task_drafts = VoiceTaskDraftService(reminders, identity, yougile)
        dispatcher = Dispatcher()
        dispatcher.include_router(create_router(
            session_factory=session_factory, yougile=yougile, digest_service=digest_service,
            personal_service=personal_service, personal_identity=identity,
            reminder_service=reminders, error_reporter=reporter,
        ))
        scheduler = create_scheduler(
            bot=bot, session_factory=session_factory, yougile=yougile,
            digest_service=digest_service, personal_service=personal_service,
            reminder_service=reminders, error_reporter=reporter,
        )
        await bot.delete_webhook(drop_pending_updates=False)
        scheduler.start()
        logger.info("Bot polling started; weekday schedule is Mon-Fri 12:00 Europe/Moscow")
        await dispatcher.start_polling(bot)
    except Exception as exc:
        await reporter.report(exc, component="runtime", operation="run")
        raise SystemExit(1) from None
    finally:
        if scheduler is not None and scheduler.running:
            scheduler.shutdown(wait=False)
        if pending_reports:
            await asyncio.gather(*pending_reports, return_exceptions=True)
        polling_logger.removeHandler(polling_handler)
        loop.set_exception_handler(old_exception_handler)
        for operation, close in (("close_yougile", yougile.aclose), ("close_openai", openai_service.aclose),
                                 ("close_database", engine.dispose)):
            try:
                await close()
            except Exception as exc:
                await reporter.report(exc, component="runtime", operation=operation)
        await bot.session.close()
        logger.info("Bot stopped")


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
