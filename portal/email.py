from __future__ import annotations

import logging
import urllib.parse
from pathlib import Path
from typing import Any

from fastapi_mail import ConnectionConfig, FastMail, MessageSchema, MessageType

from portal.config import settings

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


async def _send_email_async(
    email_to: str,
    subject: str,
    template_name: str,
    template_body: dict[str, Any],
) -> bool:
    if not _mail_config:
        logger.warning(
            "SMTP is not configured. Would have sent email to %s (subject: %s, template: %s)",
            email_to,
            subject,
            template_name,
        )
        return True

    message = MessageSchema(
        subject=subject,
        recipients=[email_to],
        template_body=template_body,
        subtype=MessageType.html,
    )
    fm = FastMail(_mail_config)
    try:
        await fm.send_message(message, template_name=template_name)
        return True
    except Exception as exc:
        logger.error("Failed to send email to %s: %s", email_to, exc, exc_info=True)
        return False


async def send_verification_email(email_to: str, token: str) -> bool:
    verification_link = f"{settings.public_base_url}/auth/verify/{token}"
    return await _send_email_async(
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


async def send_hosting_request_notification(
    email_to: str,
    *,
    requester_name: str,
    requester_email: str,
    organization: str,
    event_name: str,
    event_dates: str,
    languages: str,
    message: str,
    review_url: str,
) -> None:
    await _send_email_async(
        email_to=email_to,
        subject=f"New hosting access request from {requester_email}",
        template_name="hosting_request.html",
        template_body={
            "requester_name": requester_name,
            "requester_email": requester_email,
            "organization": organization,
            "event_name": event_name,
            "event_dates": event_dates,
            "languages": languages,
            "message": message,
            "review_url": review_url,
        },
    )


async def send_hosting_request_declined_email(email_to: str, reason: str, account_url: str) -> None:
    await _send_email_async(
        email_to=email_to,
        subject="Your hosting access request on VoxBento",
        template_name="hosting_request_declined.html",
        template_body={
            "reason": reason,
            "account_url": account_url,
        },
    )
