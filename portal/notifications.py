"""Configurable transactional email templates and a durable SMTP outbox."""

from __future__ import annotations

import asyncio
import logging
import urllib.parse
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi_mail import ConnectionConfig, FastMail, MessageSchema, MessageType
from fastapi_mail.errors import ConnectionErrors
from jinja2 import FileSystemLoader, TemplateSyntaxError, meta, nodes, select_autoescape
from jinja2.sandbox import SandboxedEnvironment
from markupsafe import Markup
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from portal.config import settings
from portal.crypto import decrypt_val, encrypt_val
from portal.database import get_session
from portal.models import AuthToken, EmailDelivery, EmailSettings, EmailTemplate, EmailTemplateRevision, utc_now

logger = logging.getLogger(__name__)

_TEMPLATE_DIR = Path(__file__).resolve().parent / "templates" / "email"
# Sandboxed: templates are editable by Admins, so they must never reach Python internals.
_env = SandboxedEnvironment(
    loader=FileSystemLoader(str(_TEMPLATE_DIR)),
    autoescape=select_autoescape(["html", "xml"]),
)
_claim_duration = timedelta(minutes=2)
_max_attempts = 4
# Subjects are plain-text mail headers, so they are not HTML-escaped ("Q&A", not "Q&amp;A").
_subject_env = SandboxedEnvironment(autoescape=False)
# Rendered content of finished deliveries is erased after this; the audit row stays.
_content_retention = timedelta(days=30)
_FINISHED = ("accepted", "failed", "expired", "suppressed", "disabled")
# The whole template grammar: text, {{ placeholder }}, {% if %}/{% elif %}/{% else %},
# and simple comparisons. No attribute access, calls, filters, loops, assignments or includes.
_ALLOWED_NODES = (
    nodes.Template, nodes.Output, nodes.TemplateData, nodes.Name, nodes.Const,
    nodes.If, nodes.Not, nodes.And, nodes.Or, nodes.Compare, nodes.Operand,
)


@dataclass(frozen=True)
class NotificationDefinition:
    key: str
    label: str
    category: str
    subject: str
    template_name: str
    variables: frozenset[str]


def _definition(
    key: str, label: str, category: str, subject: str, template_name: str, *variables: str
) -> NotificationDefinition:
    return NotificationDefinition(key, label, category, subject, template_name, frozenset(variables))


NOTIFICATIONS: tuple[NotificationDefinition, ...] = (
    _definition("verification", "Verify account", "Account", "Verify your VoxBento account", "verification.html", "verification_link"),
    _definition("magic_login", "Magic sign-in", "Account", "Sign in to VoxBento", "magic_link.html", "magic_link"),
    _definition("password_reset", "Password reset", "Account", "Reset your VoxBento password", "password_reset.html", "reset_link"),
    _definition("role_assignment", "Team invitation", "Team", "You have been invited to join VoxBento", "role_invite.html", "role_name", "context_name", "invite_url"),
    _definition("role_removed", "Team access removed", "Team", "Your VoxBento team access has changed", "role_removed.html", "context_name", "login_url"),
    _definition("hosting_invite", "Hosting invitation", "Hosting", "You've been invited to host events on VoxBento", "hosting_invite.html", "invite_url", "is_existing_user"),
    _definition("hosting_granted", "Hosting access granted", "Hosting", "Hosting access granted on VoxBento", "hosting_granted.html", "login_url"),
    _definition("hosting_request", "Hosting request received", "Hosting", "New hosting access request", "hosting_request.html", "requester_name", "requester_email", "organization", "event_name", "event_dates", "languages", "message", "review_url"),
    _definition("hosting_request_declined", "Hosting request declined", "Hosting", "Your VoxBento hosting request", "hosting_request_declined.html", "reason", "account_url"),
    _definition("onboarding", "Welcome", "Account", "Welcome to VoxBento", "onboarding.html", "display_name"),
    _definition("demo_request", "Demo request", "Sales", "New VoxBento demo request", "demo_request.html", "firstName", "lastName", "email", "companyName", "phoneNumber", "country", "industry", "companySize", "languageSupport", "notes", "admin_url"),
    _definition("account_access", "Account access changed", "Account", "Your VoxBento access has changed", "account_access.html", "message", "login_url"),
    _definition("developer_account", "Developer account update", "Developer", "Your VoxBento developer account", "developer_account.html", "message", "login_url"),
)
NOTIFICATION_BY_KEY = {definition.key: definition for definition in NOTIFICATIONS}


def smtp_configured() -> bool:
    return bool(settings.smtp_host)


def _mail_config() -> ConnectionConfig:
    return ConnectionConfig(
        MAIL_USERNAME=settings.smtp_user,
        MAIL_PASSWORD=settings.smtp_password,
        MAIL_FROM=settings.smtp_from_email,
        MAIL_PORT=settings.smtp_port,
        MAIL_SERVER=settings.smtp_host,
        MAIL_STARTTLS=settings.smtp_port == 587,
        MAIL_SSL_TLS=settings.smtp_port == 465,
        USE_CREDENTIALS=bool(settings.smtp_user and settings.smtp_password),
        VALIDATE_CERTS=True,
    )


def get_definition(key: str) -> NotificationDefinition:
    definition = NOTIFICATION_BY_KEY.get(key)
    if definition is None:
        raise ValueError("Unknown email notification type.")
    return definition


def _default_html(definition: NotificationDefinition) -> str:
    return _env.get_template(definition.template_name).render()


def _source_for_default(definition: NotificationDefinition) -> str:
    source, _, _ = _env.loader.get_source(_env, definition.template_name)
    return source


def _validate_template(definition: NotificationDefinition, subject: str, html: str) -> None:
    if not subject.strip() or not html.strip():
        raise ValueError("Subject and email content are required.")
    try:
        parsed_html = _env.parse(html)
        parsed_subject = _env.parse(subject)
    except TemplateSyntaxError as exc:
        raise ValueError(f"Template syntax error: {exc.message}") from exc
    for tree in (parsed_html, parsed_subject):
        for node in tree.find_all(nodes.Node):
            if not isinstance(node, _ALLOWED_NODES):
                raise ValueError(
                    "Email templates may only use {{ placeholders }} and {% if %} conditions "
                    "(no filters, attributes, calls, loops or includes)."
                )
    variables = meta.find_undeclared_variables(parsed_html) | meta.find_undeclared_variables(parsed_subject)
    unsupported = variables - definition.variables
    if unsupported:
        raise ValueError("This email uses unsupported placeholders: " + ", ".join(sorted(unsupported)))


def _render(definition: NotificationDefinition, subject: str, html: str, context: Mapping[str, Any]) -> tuple[str, str]:
    _validate_template(definition, subject, html)
    rendered_subject = _subject_env.from_string(subject).render(**context)
    # A subject is a mail header: user-supplied values must not add lines to it.
    rendered_subject = " ".join(rendered_subject.split())
    return rendered_subject, _env.from_string(html).render(**context)


def default_logo_url(*, preview: bool = False) -> str:
    # Browser previews use this checkout's asset; delivered mail needs a public URL.
    base = "" if preview else settings.public_base_url.rstrip("/")
    return f"{base}/static/img/email-mark.png"


async def email_branding(session: AsyncSession) -> EmailSettings:
    """The saved branding, or the defaults (VoxBento logo shown) when nothing was saved."""
    return await session.get(EmailSettings, 1) or EmailSettings(id=1, show_logo=True, logo_url=None)


def _validate_logo_url(url: str) -> str:
    url = url.strip()
    if not url:
        return ""
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" or not parsed.netloc or len(url) > 500 or any(c in url for c in "\"'<> \t\r\n"):
        raise ValueError("The logo must be an https:// image address.")
    return url


async def save_email_branding(session: AsyncSession, *, show_logo: bool, logo_url: str, editor_id: int) -> None:
    logo_url = _validate_logo_url(logo_url)  # before changing anything
    branding = await session.get(EmailSettings, 1)
    if branding is None:
        branding = EmailSettings(id=1)
        session.add(branding)
    branding.show_logo = show_logo
    branding.logo_url = logo_url or None
    branding.updated_by_id = editor_id


async def _wrap(session: AsyncSession, subject: str, body: str, *, preview: bool = False) -> str:
    """Put a rendered message inside the shared layout (logo, card and footer)."""
    branding = await email_branding(session)
    site = urllib.parse.urlparse(settings.public_base_url)
    return _env.get_template("_layout.html").render(
        subject=subject,
        body=Markup(body),  # noqa: S704  # nosec B704 - already rendered with autoescaping from an allowlisted template
        logo_url=(branding.logo_url or default_logo_url(preview=preview)) if branding.show_logo else "",
        default_logo=not branding.logo_url,
        site_url=settings.public_base_url.rstrip("/"),
        site_host=site.netloc,
    )


async def template_content(session: AsyncSession, key: str, *, draft: bool = False) -> tuple[str, str, int]:
    definition = get_definition(key)
    template = await session.get(EmailTemplate, key)
    if template is None:
        return definition.subject, _source_for_default(definition), 0
    if draft and template.draft_subject and template.draft_html:
        return template.draft_subject, template.draft_html, template.published_version
    if template.published_subject and template.published_html:
        return template.published_subject, template.published_html, template.published_version
    return definition.subject, _source_for_default(definition), 0


async def save_draft(session: AsyncSession, key: str, subject: str, html: str, editor_id: int) -> None:
    definition = get_definition(key)
    _validate_template(definition, subject, html)
    template = await session.get(EmailTemplate, key)
    if template is None:
        template = EmailTemplate(key=key)
        session.add(template)
    template.draft_subject = subject.strip()
    template.draft_html = html.strip()
    template.updated_by_id = editor_id


async def publish_draft(session: AsyncSession, key: str, editor_id: int) -> int:
    template = await session.get(EmailTemplate, key)
    if template is None or not template.draft_subject or not template.draft_html:
        raise ValueError("Save a draft before publishing it.")
    definition = get_definition(key)
    _validate_template(definition, template.draft_subject, template.draft_html)
    template.published_version += 1
    template.published_subject = template.draft_subject
    template.published_html = template.draft_html
    template.updated_by_id = editor_id
    session.add(
        EmailTemplateRevision(
            template_key=key,
            version=template.published_version,
            subject=template.published_subject,
            html=template.published_html,
            published_by_id=editor_id,
        )
    )
    return template.published_version


async def queue_notification(
    session: AsyncSession,
    key: str,
    recipient: str,
    context: Mapping[str, Any],
    *,
    scheduled_at: datetime | None = None,
    expires_at: datetime | None = None,
    idempotency_key: str | None = None,
    auth_token_id: int | None = None,
) -> EmailDelivery:
    """Render now and persist a message in the same transaction as its trigger."""
    definition = get_definition(key)
    if idempotency_key:
        existing = await session.scalar(select(EmailDelivery).where(EmailDelivery.idempotency_key == idempotency_key))
        if existing is not None:
            return existing
    subject, html, version = await template_content(session, key)
    rendered_subject, rendered_html = _render(definition, subject, html, context)
    rendered_html = await _wrap(session, rendered_subject, rendered_html)
    delivery = EmailDelivery(
        notification_key=key,
        recipient=recipient.strip().lower(),
        encrypted_subject=encrypt_val(rendered_subject) or "",
        encrypted_html=encrypt_val(rendered_html) or "",
        template_version=version,
        scheduled_at=scheduled_at or utc_now(),
        expires_at=expires_at,
        auth_token_id=auth_token_id,
        idempotency_key=idempotency_key,
    )
    session.add(delivery)
    await session.flush()
    return delivery


async def preview_notification(session: AsyncSession, key: str, context: Mapping[str, Any], *, draft: bool = True) -> str:
    definition = get_definition(key)
    subject, html, _ = await template_content(session, key, draft=draft)
    rendered_subject, rendered_html = _render(definition, subject, html, context)
    return await _wrap(session, rendered_subject, rendered_html, preview=True)


async def _claim_delivery() -> int | None:
    now = utc_now()
    async with get_session() as session:
        await session.execute(
            EmailDelivery.__table__.update()
            .where(EmailDelivery.status == "sending", EmailDelivery.claim_until < now)
            .values(status="pending", claimed_at=None, claim_until=None)
        )
        await session.execute(
            EmailDelivery.__table__.update()
            .where(EmailDelivery.status == "pending", EmailDelivery.expires_at.is_not(None), EmailDelivery.expires_at <= now)
            .values(status="expired", last_error="Expired before it could be sent.")
        )
        delivery = await session.scalar(
            select(EmailDelivery)
            .where(
                EmailDelivery.status == "pending",
                EmailDelivery.scheduled_at <= now,
                or_(EmailDelivery.expires_at.is_(None), EmailDelivery.expires_at > now),
            )
            .order_by(EmailDelivery.scheduled_at, EmailDelivery.id)
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        if delivery is None:
            return None
        delivery.status = "sending"
        delivery.attempt_count += 1
        delivery.claimed_at = now
        delivery.claim_until = now + _claim_duration
        return delivery.id


async def _send_delivery(delivery_id: int) -> None:
    try:
        async with get_session() as session:
            delivery = await session.get(EmailDelivery, delivery_id)
            if delivery is None or delivery.status != "sending":
                return
            if delivery.auth_token_id is not None:
                token = await session.get(AuthToken, delivery.auth_token_id)
                if token is None or token.is_used or token.is_expired:
                    delivery.status = "suppressed"
                    delivery.claim_until = None
                    delivery.last_error = "The linked authentication token is no longer valid."
                    return
            recipient = delivery.recipient
            try:
                subject = decrypt_val(delivery.encrypted_subject) or ""
                html = decrypt_val(delivery.encrypted_html) or ""
            except ValueError:
                # Retrying can't help: the key that encrypted it is gone.
                delivery.status = "failed"
                delivery.failed_at = utc_now()
                delivery.claim_until = None
                delivery.last_error = (
                    "Couldn't decrypt this queued email: API_KEY_ENCRYPTION_KEY changed after it was queued. "
                    "Keep the old key in the list when rotating."
                )
                return
            if not subject or not html:
                delivery.status = "failed"
                delivery.failed_at = utc_now()
                delivery.claim_until = None
                delivery.last_error = "This email's content was already erased."
                return
        if not smtp_configured():
            async with get_session() as session:
                delivery = await session.get(EmailDelivery, delivery_id)
                if delivery is not None and delivery.status == "sending":
                    delivery.status = "disabled"
                    delivery.last_error = "SMTP is not configured."
            logger.warning("Email delivery %s was retained because SMTP is not configured", delivery_id)
            return
        message = MessageSchema(subject=subject, recipients=[recipient], body=html, subtype=MessageType.html)
        await FastMail(_mail_config()).send_message(message)
    except (ConnectionErrors, ConnectionError, TimeoutError, OSError, ValueError) as exc:
        await _record_failure(delivery_id, str(exc))
        logger.warning("Email delivery %s failed and will be retried", delivery_id)
        return
    except Exception as exc:  # noqa: BLE001 - an unexpected SMTP error must not lose the message
        await _record_failure(delivery_id, f"{type(exc).__name__}: {exc}")
        logger.exception("Email delivery %s failed unexpectedly and will be retried", delivery_id)
        return
    async with get_session() as session:
        delivery = await session.get(EmailDelivery, delivery_id)
        if delivery is not None and delivery.status == "sending":
            delivery.status = "accepted"
            delivery.accepted_at = utc_now()
            delivery.claim_until = None
            delivery.last_error = None


async def _record_failure(delivery_id: int, error: str) -> None:
    async with get_session() as session:
        delivery = await session.get(EmailDelivery, delivery_id)
        if delivery is None or delivery.status != "sending":
            return
        delivery.last_error = error[:1000]
        delivery.claim_until = None
        if delivery.attempt_count >= _max_attempts:
            delivery.status = "failed"
            delivery.failed_at = utc_now()
            return
        delivery.status = "pending"
        delay = min(300, 15 * (2 ** (delivery.attempt_count - 1)))
        delivery.scheduled_at = utc_now() + timedelta(seconds=delay)


async def purge_old_content() -> int:
    """Erase the rendered subject and body of deliveries finished over the retention period."""
    cutoff = utc_now() - _content_retention
    async with get_session() as session:
        result = await session.execute(
            EmailDelivery.__table__.update()
            .where(
                EmailDelivery.status.in_(_FINISHED),
                EmailDelivery.created_at < cutoff,
                EmailDelivery.encrypted_html != "",
            )
            .values(encrypted_subject="", encrypted_html="")
        )
        return result.rowcount or 0


async def process_due_deliveries(limit: int = 20) -> int:
    processed = 0
    for _ in range(limit):
        delivery_id = await _claim_delivery()
        if delivery_id is None:
            break
        await _send_delivery(delivery_id)
        processed += 1
    return processed


async def notification_worker_loop() -> None:
    """Keep processing the outbox; message rows survive restarts and crashes."""
    last_purge = None
    while True:
        try:
            if last_purge is None or utc_now() - last_purge > timedelta(hours=1):
                last_purge = utc_now()  # set first: a failing purge is retried hourly, not every loop
                try:
                    await purge_old_content()
                except Exception:  # noqa: BLE001 - erasing old content must never block sending
                    logger.exception("Email content purge failed")
            processed = await process_due_deliveries()
            await asyncio.sleep(1 if processed else 10)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - the worker must keep running; rows are retried
            logger.exception("Email outbox worker error")
            await asyncio.sleep(10)
