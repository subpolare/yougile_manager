from __future__ import annotations

import random
from datetime import date

from sqlalchemy.exc import IntegrityError

from app.db import SessionFactory
from app.greetings import DUMB_GREETINGS
from app.models import DailyDigestGreeting


class DailyGreetingProvider:
    def __init__(self, session_factory: SessionFactory) -> None:
        self.session_factory = session_factory

    async def get(self, digest_date: date) -> str:
        async with self.session_factory() as session:
            existing = await session.get(DailyDigestGreeting, digest_date)
            if existing is not None:
                return existing.greeting
        try:
            async with self.session_factory() as session, session.begin():
                session.add(DailyDigestGreeting(
                    digest_date=digest_date, greeting=random.choice(DUMB_GREETINGS)
                ))
                await session.flush()
        except IntegrityError:
            # A concurrent insert won. Read in a new transaction, including on
            # MySQL REPEATABLE READ, so both workers use the committed winner.
            pass
        async with self.session_factory() as session:
            row = await session.get(DailyDigestGreeting, digest_date)
            if row is None:
                raise RuntimeError("Daily greeting was not persisted")
            return row.greeting
