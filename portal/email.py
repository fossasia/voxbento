from __future__ import annotations

import asyncio
import logging
import urllib.parse
from datetime import timedelta
from pathlib import Path
from typing import Any

from fastapi_mail import ConnectionConfig, FastMail, MessageSchema, MessageType
from sqlalchemy import select, update
from sqlalchemy.exc import SQLAlchemyError

from portal.config import settings
from portal.database import get_session
from portal.models import User, utc_now

_BASE_DIR = Path(__file__).resolve().parent
_TEMPLATE_DIR = _BASE_DIR / "templates" / "email"

# If SMTP host is not configured, we'll log the email instead of failing.
_mail_config = None
if settings.smtp_host:
    _mail_config = ConnectionConfig(
        MAIL_USERNAME=settings.smtp_user,
        MAIL_PASSWORD=settings.smtp_password,
        MAIL_FROM=settings.smtp_from_email,
        MAIL_PORT=settings.smtp_port,
        MAIL_SERVER=settings.smtp_host,
        MAIL_STARTTLS=True if settings.smtp_port == 587 else False,
        MAIL_SSL_TLS=True if settings.smtp_port == 465 else False,
        USE_CREDENTIALS=bool(settings.smtp_user and settings.smtp_password),
        VALIDATE_CERTS=True,
        TEMPLATE_FOLDER=str(_TEMPLATE_DIR),
    )

logger = logging.getLogger(__name__)

ONBOARDING_EMAIL_DELAY = timedelta(minutes=15)
ONBOARDING_POLL_SECS = 60
_ONBOARDING_FROM_NAME = "Arnav Angarkar, Team Voxbento"


async def _send_message(message: MessageSchema, template_name: str | None = None) -> bool:
    email_to = ", ".join(r.email for r in message.recipients)
    if not _mail_config:
        logger.warning(
            "SMTP is not configured. Would have sent email to %s (subject: %s, template: %s)",
            email_to,
            message.subject,
            template_name,
        )
        return True

    fm = FastMail(_mail_config)
    try:
        await fm.send_message(message, template_name=template_name)
        return True
    except Exception as exc:
        logger.error("Failed to send email to %s: %s", email_to, exc, exc_info=True)
        return False


async def _send_email_async(
    email_to: str,
    subject: str,
    template_name: str,
    template_body: dict[str, Any],
) -> bool:
    message = MessageSchema(
        subject=subject,
        recipients=[email_to],
        template_body=template_body,
        subtype=MessageType.html,
    )
    return await _send_message(message, template_name)


async def send_verification_email(email_to: str, token: str) -> None:
    verification_link = f"{settings.public_base_url}/auth/verify/{token}"
    await _send_email_async(
        email_to=email_to,
        subject="Verify your VoxBento account",
        template_name="verification.html",
        template_body={"verification_link": verification_link},
    )


async def send_magic_login_email(email_to: str, token: str, next_url: str = "") -> bool:
    magic_link = f"{settings.public_base_url}/auth/magic/{token}"
    if next_url:
        magic_link += f"?next={urllib.parse.quote(next_url)}"
    return await _send_email_async(
        email_to=email_to,
        subject="Sign in to VoxBento",
        template_name="magic_link.html",
        template_body={"magic_link": magic_link},
    )


async def send_password_reset_email(email_to: str, token: str) -> None:
    reset_url = f"{settings.public_base_url}/auth/reset/{token}"
    await _send_email_async(
        email_to=email_to,
        subject="Reset your VoxBento password",
        template_name="password_reset.html",
        template_body={"reset_link": reset_url},
    )


async def send_role_invite_email(email_to: str, invite_url: str, role_name: str, context_name: str) -> None:
    await _send_email_async(
        email_to=email_to,
        subject=f"You have been invited to join as {role_name} on VoxBento",
        template_name="role_invite.html",
        template_body={
            "role_name": role_name,
            "context_name": context_name,
            "invite_url": invite_url,
        },
    )


async def send_hosting_invite_email(email_to: str, invite_url: str, is_existing_user: bool = False) -> None:
    await _send_email_async(
        email_to=email_to,
        subject="You've been invited to host events on VoxBento",
        template_name="hosting_invite.html",
        template_body={
            "invite_url": invite_url,
            "is_existing_user": is_existing_user,
        },
    )


async def send_hosting_granted_email(email_to: str, login_url: str) -> None:
    await _send_email_async(
        email_to=email_to,
        subject="Hosting access granted on VoxBento",
        template_name="hosting_granted.html",
        template_body={
            "login_url": login_url,
        },
    )


async def send_demo_request_email(form_data: dict[str, Any]) -> None:
    user_id = form_data.get("user_id")
    admin_base = f"{settings.public_base_url.rstrip('/')}/admin/users/"
    admin_url = f"{admin_base}{user_id}/" if user_id else admin_base
    message = MessageSchema(
        subject=f"New Demo Request from {form_data.get('email', 'Unknown')}",
        recipients=[settings.team_email],
        template_body={**form_data, "admin_url": admin_url},
        subtype=MessageType.html,
    )
    await _send_message(message, "demo_request.html")


async def send_onboarding_email(email_to: str, display_name: str) -> None:
    message = MessageSchema(
        subject="Welcome to VoxBento! How can we help?",
        recipients=[email_to],
        template_body={"display_name": display_name, "email_address": email_to},
        subtype=MessageType.html,
        from_name=_ONBOARDING_FROM_NAME,
        reply_to=[settings.team_email],
    )
    await _send_message(message, "onboarding.html")


def schedule_onboarding_email(user: User) -> None:
    user.onboarding_email_due_at = utc_now() + ONBOARDING_EMAIL_DELAY


async def send_due_onboarding_emails() -> None:
    # Clearing the due time before sending claims each row once, even with several workers.
    # One row per transaction, so a crash mid-send loses at most the email in flight.
    while True:
        async with get_session() as session:
            next_due = select(User.id).where(User.onboarding_email_due_at <= utc_now()).limit(1)
            result = await session.execute(
                update(User)
                .where(User.id.in_(next_due), User.onboarding_email_due_at.is_not(None))
                .values(onboarding_email_due_at=None)
                .returning(User.email, User.display_name)
            )
            claimed = result.first()
        if claimed is None:
            return
        await send_onboarding_email(*claimed)


async def onboarding_email_loop() -> None:
    while True:
        try:
            await send_due_onboarding_emails()
        except SQLAlchemyError:
            logger.warning("onboarding_email database_unavailable")
        except Exception:  # noqa: BLE001 - the loop must outlive any single bad tick
            logger.exception("onboarding_email dispatch_error")
        await asyncio.sleep(ONBOARDING_POLL_SECS)
