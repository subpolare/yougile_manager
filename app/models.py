from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import BigInteger, Boolean, Date, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class ChatProjectBinding(Base):
    __tablename__ = "chat_project_bindings"

    telegram_chat_id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=False
    )
    yougile_project_id: Mapped[str] = mapped_column(String(36), unique=True, nullable=False)
    project_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    project_title: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class DailyDispatch(Base):
    __tablename__ = "daily_dispatches"
    __table_args__ = (
        UniqueConstraint("telegram_chat_id", "dispatch_date", name="uq_daily_dispatch_chat_date"),
    )

    telegram_chat_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    dispatch_date: Mapped[date] = mapped_column(Date, primary_key=True)
    sent_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class PersonalDigestSubscription(Base):
    __tablename__ = "personal_digest_subscriptions"

    telegram_user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    yougile_user_id: Mapped[str] = mapped_column(String(36), unique=True, nullable=False)
    telegram_username: Mapped[str | None] = mapped_column(String(64))
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )


class DailyDigestGreeting(Base):
    __tablename__ = "daily_digest_greetings"

    digest_date: Mapped[date] = mapped_column(Date, primary_key=True)
    greeting: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


# UTC-naive DATETIME values: MySQL does not preserve timezone information.
class VoiceReminder(Base):
    __tablename__ = "voice_reminders"
    __table_args__ = (Index("ix_voice_owner_order", "telegram_user_id", "created_at", "id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    telegram_user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    original_voice_chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    original_voice_message_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    transcript: Mapped[str] = mapped_column(Text, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class ReminderDelivery(Base):
    __tablename__ = "reminder_deliveries"
    __table_args__ = (
        UniqueConstraint("telegram_chat_id", "telegram_message_id", name="uq_reminder_message"),
        UniqueConstraint("reminder_id", "scheduled_date", name="uq_reminder_schedule"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    reminder_id: Mapped[int] = mapped_column(
        ForeignKey("voice_reminders.id", ondelete="CASCADE"), nullable=False
    )
    telegram_chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    telegram_message_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sent_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    scheduled_date: Mapped[date | None] = mapped_column(Date)


class ReminderEditSession(Base):
    __tablename__ = "reminder_edit_sessions"

    telegram_user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    reminder_id: Mapped[int] = mapped_column(
        ForeignKey("voice_reminders.id", ondelete="CASCADE"), nullable=False, index=True
    )
    prompt_chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    prompt_message_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class ErrorAdminTarget(Base):
    __tablename__ = "error_admin_targets"

    identity: Mapped[str] = mapped_column(String(32), primary_key=True)
    telegram_user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
