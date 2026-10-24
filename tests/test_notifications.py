"""Tests for the durable, configurable transactional email outbox."""

from __future__ import annotations

from html.parser import HTMLParser

import pytest
from sqlalchemy import select

from portal.database import configure, create_auth_token, create_user, dispose, get_session, init_db, revoke_auth_token
from portal.models import EmailDelivery, EmailTemplateRevision
from portal.notifications import process_due_deliveries, publish_draft, queue_notification, save_draft


@pytest.fixture(autouse=True)
async def setup_db():
    configure("sqlite+aiosqlite://")
    await init_db()
    yield
    await dispose()


@pytest.mark.anyio
async def test_notification_is_encrypted_and_retained_when_smtp_is_unconfigured(monkeypatch):
    monkeypatch.setattr("portal.notifications.settings.smtp_host", "")
    async with get_session() as session:
        delivery = await queue_notification(
            session,
            "verification",
            "person@example.com",
            {"verification_link": "https://example.invalid/auth/verify/secret"},
            idempotency_key="verification:test",
        )
        delivery_id = delivery.id

    assert await process_due_deliveries() == 1

    async with get_session() as session:
        stored = await session.get(EmailDelivery, delivery_id)
    assert stored is not None
    assert stored.status == "disabled"
    assert "secret" not in stored.encrypted_html


@pytest.mark.anyio
async def test_delivery_is_suppressed_when_its_link_token_is_revoked(monkeypatch):
    monkeypatch.setattr("portal.notifications.settings.smtp_host", "smtp.example.invalid")
    async with get_session() as session:
        user = await create_user(session, email="person@example.com", display_name="Person")
        token = await create_auth_token(session, user_id=user.id, token_type="verification", jti="revoked-link-token")
        delivery = await queue_notification(
            session,
            "verification",
            user.email,
            {"verification_link": "https://example.invalid/auth/verify/revoked-link-token"},
            auth_token_id=token.id,
        )
        await revoke_auth_token(session, jti=token.jti)
        delivery_id = delivery.id

    assert await process_due_deliveries() == 1

    async with get_session() as session:
        stored = await session.get(EmailDelivery, delivery_id)
    assert stored is not None
    assert stored.status == "suppressed"


@pytest.mark.anyio
async def test_templates_reject_unknown_variables_and_publish_an_audited_revision():
    async with get_session() as session:
        editor = await create_user(session, email="editor@example.com", display_name="Editor")
        editor_id = editor.id
    async with get_session() as session:
        with pytest.raises(ValueError, match="unsupported placeholders"):
            await save_draft(session, "verification", "Hello {{ unsupported }}", "<p>Hi</p>", editor_id=editor_id)

    async with get_session() as session:
        await save_draft(
            session,
            "verification",
            "Confirm your account",
            "<p><a href=\"{{ verification_link }}\">Confirm</a></p>",
            editor_id=editor_id,
        )
        assert await publish_draft(session, "verification", editor_id=editor_id) == 1

    async with get_session() as session:
        revisions = list((await session.scalars(select(EmailTemplateRevision))).all())
    assert len(revisions) == 1
    assert revisions[0].version == 1


# ── Template grammar, sandbox and headers ────────────────────────────────


@pytest.mark.anyio
@pytest.mark.parametrize(
    "html",
    [
        "{{ verification_link.__class__.__mro__ }}",
        "{{ verification_link['x'] }}",
        "{{ verification_link|upper }}",
        "{{ lipsum() }}",
        "{% for x in verification_link %}{{ x }}{% endfor %}",
        "{% set x = 1 %}",
        "{% include 'magic_link.html' %}",
        "{% with x = 1 %}{{ x }}{% endwith %}",
    ],
)
async def test_templates_allow_only_placeholders_and_conditions(html):
    async with get_session() as session:
        with pytest.raises(ValueError, match="only use"):
            await save_draft(session, "verification", "Confirm", html, editor_id=1)


@pytest.mark.anyio
async def test_conditions_and_every_default_template_are_accepted():
    from portal.notifications import NOTIFICATIONS, template_content

    async with get_session() as session:
        editor = await create_user(session, email="editor@example.com", display_name="Editor")
        await save_draft(
            session,
            "hosting_invite",
            "Join us",
            "{% if is_existing_user %}<p>Sign in</p>{% else %}<p>Create an account</p>{% endif %}{{ invite_url }}",
            editor_id=editor.id,
        )
        for definition in NOTIFICATIONS:
            subject, html, _ = await template_content(session, definition.key)
            # Raises if a built-in template uses anything outside the allowed grammar.
            from portal.notifications import _validate_template

            _validate_template(definition, subject, html)


@pytest.mark.anyio
async def test_subject_cannot_gain_extra_header_lines():
    async with get_session() as session:
        editor = await create_user(session, email="editor@example.com", display_name="Editor")
        await save_draft(
            session, "hosting_request", "Request from {{ requester_name }}", "<p>{{ requester_name }}</p>", editor_id=editor.id
        )
        await publish_draft(session, "hosting_request", editor_id=editor.id)
        delivery = await queue_notification(
            session,
            "hosting_request",
            "team@example.com",
            {"requester_name": "Eve\r\nBcc: victim@example.com"},
        )
        from portal.crypto import decrypt_val

        subject = decrypt_val(delivery.encrypted_subject)
    assert "\r" not in subject and "\n" not in subject
    assert subject == "Request from Eve Bcc: victim@example.com"


# ── Worker resilience, expiry and retention ──────────────────────────────


@pytest.mark.anyio
async def test_unexpected_send_error_is_recorded_and_retried(monkeypatch):
    monkeypatch.setattr("portal.notifications.settings.smtp_host", "smtp.example.invalid")

    class Boom(Exception):
        pass

    async def fail(self, message):
        raise Boom("server said no")

    monkeypatch.setattr("portal.notifications.FastMail.send_message", fail)
    async with get_session() as session:
        delivery = await queue_notification(
            session, "verification", "person@example.com", {"verification_link": "https://example.invalid/v"}
        )
        delivery_id = delivery.id

    assert await process_due_deliveries() == 1
    async with get_session() as session:
        stored = await session.get(EmailDelivery, delivery_id)
    assert stored.status == "pending"
    assert stored.attempt_count == 1
    assert "Boom" in stored.last_error


@pytest.mark.anyio
async def test_worker_loop_survives_unexpected_errors(monkeypatch):
    import asyncio

    import portal.notifications as notifications

    calls = 0

    async def flaky(limit=20):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("unexpected")
        raise asyncio.CancelledError

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(notifications, "process_due_deliveries", flaky)
    monkeypatch.setattr(notifications.asyncio, "sleep", no_sleep)
    with pytest.raises(asyncio.CancelledError):
        await notifications.notification_worker_loop()
    assert calls == 2


@pytest.mark.anyio
async def test_expired_messages_are_marked_and_old_content_is_erased():
    from datetime import timedelta

    from portal.models import utc_now
    from portal.notifications import purge_old_content

    async with get_session() as session:
        expired = await queue_notification(
            session, "verification", "a@example.com", {"verification_link": "https://example.invalid/v"},
            expires_at=utc_now() - timedelta(minutes=1),
        )
        old = await queue_notification(
            session, "verification", "b@example.com", {"verification_link": "https://example.invalid/v"}
        )
        old.status = "accepted"
        old.created_at = utc_now() - timedelta(days=31)
        ids = (expired.id, old.id)

    await process_due_deliveries()
    assert await purge_old_content() == 1
    async with get_session() as session:
        expired_row = await session.get(EmailDelivery, ids[0])
        old_row = await session.get(EmailDelivery, ids[1])
    assert expired_row.status == "expired"
    assert old_row.encrypted_html == "" and old_row.encrypted_subject == ""
    assert old_row.recipient == "b@example.com"


# ── Admin → Emails ───────────────────────────────────────────────────────


@pytest.fixture
async def admin_cookies():
    from portal.auth import create_user_token

    async with get_session() as session:
        admin = await create_user(session, email="admin@example.com", display_name="Admin", is_admin=True)
        admin_id = admin.id
    return {"user_token": create_user_token(user_id=admin_id, email="admin@example.com", is_admin=True)}


def _client():
    from httpx import ASGITransport, AsyncClient

    from fastapi_app import app

    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.mark.anyio
async def test_every_email_opens_and_previews(admin_cookies):
    from portal.notifications import NOTIFICATIONS

    async with _client() as http:
        for definition in NOTIFICATIONS:
            page = await http.get(f"/admin/emails/{definition.key}/", cookies=admin_cookies)
            preview = await http.get(f"/admin/emails/{definition.key}/preview", cookies=admin_cookies)
            assert page.status_code == 200, definition.key
            assert preview.status_code == 200, definition.key
            assert "sandbox" in preview.headers["content-security-policy"]


@pytest.mark.anyio
async def test_unknown_email_is_404_and_invalid_draft_shows_why(admin_cookies):
    origin = {"origin": "http://test"}
    async with _client() as http:
        assert (await http.get("/admin/emails/nope/", cookies=admin_cookies)).status_code == 404
        assert (await http.post("/admin/emails/nope/draft", data={"subject": "x", "html": "y"}, headers=origin, cookies=admin_cookies)).status_code == 404
        bad = await http.post(
            "/admin/emails/verification/draft",
            data={"subject": "Hi", "html": "{{ verification_link|upper }}"},
            headers=origin,
            cookies=admin_cookies,
        )
        publish = await http.post("/admin/emails/verification/publish", headers=origin, cookies=admin_cookies)
    assert bad.status_code == 400
    assert "only use" in bad.text
    # What was typed is kept so it can be corrected.
    assert "verification_link|upper" in bad.text
    assert publish.status_code == 400
    assert "Save a draft before publishing it." in publish.text


@pytest.mark.anyio
async def test_email_that_cannot_be_decrypted_fails_at_once_with_a_clear_reason(monkeypatch):
    monkeypatch.setattr("portal.notifications.settings.smtp_host", "smtp.example.invalid")
    async with get_session() as session:
        delivery = await queue_notification(
            session, "verification", "person@example.com", {"verification_link": "https://example.invalid/v"}
        )
        delivery_id = delivery.id

    def wrong_key(_value):
        raise ValueError("Failed to decrypt API key.")

    monkeypatch.setattr("portal.notifications.decrypt_val", wrong_key)
    assert await process_due_deliveries() == 1
    async with get_session() as session:
        stored = await session.get(EmailDelivery, delivery_id)
    assert stored.status == "failed"
    assert stored.attempt_count == 1
    assert "API_KEY_ENCRYPTION_KEY changed" in stored.last_error


# ── Shared layout and branding ───────────────────────────────────────────


async def _html_of(delivery_id):
    from portal.crypto import decrypt_val

    async with get_session() as session:
        stored = await session.get(EmailDelivery, delivery_id)
        return decrypt_val(stored.encrypted_html)


@pytest.mark.anyio
async def test_every_email_uses_the_layout_with_the_logo_by_default():
    from portal.notifications import NOTIFICATIONS, default_logo_url
    from portal.routers.admin import _email_sample_context

    async with get_session() as session:
        ids = []
        for definition in NOTIFICATIONS:
            delivery = await queue_notification(session, definition.key, "a@example.com", _email_sample_context(definition.key))
            ids.append(delivery.id)
    for delivery_id in ids:
        html = await _html_of(delivery_id)
        assert f'src="{default_logo_url()}"' in html
        assert '/static/img/email-mark.png"' in html
        assert "Sent by VoxBento" in html
        # Inline styles only: many email clients drop <style> blocks.
        assert "<style" not in html


@pytest.mark.anyio
async def test_default_logo_preview_loads_from_this_site(admin_cookies, monkeypatch):
    monkeypatch.setattr("portal.notifications.settings.public_base_url", "https://mail.example.invalid/")

    class Images(HTMLParser):
        def __init__(self):
            super().__init__()
            self.sources = []

        def handle_starttag(self, tag, attrs):
            if tag == "img":
                self.sources.append(dict(attrs).get("src"))

    async with _client() as http:
        for path in ("/admin/emails/", "/admin/emails/verification/preview"):
            page = await http.get(path, cookies=admin_cookies)
            images = Images()
            images.feed(page.text)
            assert "/static/img/email-mark.png" in images.sources
        logo = await http.get("/static/img/email-mark.png")
        assert logo.status_code == 200
        assert logo.headers["content-type"] == "image/png"


@pytest.mark.anyio
async def test_branding_can_hide_the_logo_or_use_a_custom_one():
    from portal.notifications import save_email_branding

    async with get_session() as session:
        editor = await create_user(session, email="editor@example.com", display_name="Editor")
        with pytest.raises(ValueError, match="https"):
            await save_email_branding(session, show_logo=True, logo_url="http://example.com/logo.png", editor_id=editor.id)
        with pytest.raises(ValueError, match="https"):
            await save_email_branding(session, show_logo=True, logo_url='https://x.example/"onerror="x', editor_id=editor.id)
        await save_email_branding(session, show_logo=True, logo_url="https://cdn.example.com/logo.png", editor_id=editor.id)
        custom = await queue_notification(session, "magic_login", "a@example.com", {"magic_link": "https://example.invalid/m"})
        await save_email_branding(session, show_logo=False, logo_url="", editor_id=editor.id)
        hidden = await queue_notification(session, "magic_login", "b@example.com", {"magic_link": "https://example.invalid/m"})
        ids = (custom.id, hidden.id)
    assert 'src="https://cdn.example.com/logo.png"' in await _html_of(ids[0])
    assert "<img" not in await _html_of(ids[1])


@pytest.mark.anyio
async def test_branding_form_saves_and_explains_a_bad_address(admin_cookies):
    origin = {"origin": "http://test"}
    async with _client() as http:
        bad = await http.post(
            "/admin/emails/branding", data={"show_logo": "on", "logo_url": "ftp://x/logo.png"}, headers=origin, cookies=admin_cookies
        )
        ok = await http.post(
            "/admin/emails/branding", data={"logo_url": ""}, headers=origin, cookies=admin_cookies, follow_redirects=False
        )
        page = await http.get("/admin/emails/", cookies=admin_cookies)
    assert bad.status_code == 400 and "https:// image address" in bad.text
    assert ok.status_code == 303
    assert "No logo" in page.text


# ── Lifecycle emails ─────────────────────────────────────────────────────


async def _deliveries(key):
    async with get_session() as session:
        return list((await session.scalars(select(EmailDelivery).where(EmailDelivery.notification_key == key))).all())


@pytest.mark.anyio
async def test_removing_a_team_role_tells_the_person_where():
    from portal.database import create_booth, create_event, create_room, set_booth_membership, set_event_membership
    from portal.team import remove_assignment

    async with get_session() as session:
        owner = await create_user(session, email="owner@example.com", display_name="Owner")
        interp = await create_user(session, email="interp@example.com", display_name="Interp")
        event = await create_event(session, slug="summit", display_name="Summit")
        room = await create_room(session, event_id=event.id, display_name="Main Hall")
        booth = await create_booth(session, event_id=event.id, room_id=room.id, language_code="de", language_name="Deutsch")
        await set_event_membership(session, user_id=owner.id, event_id=event.id, role="event_owner")
        row = await set_booth_membership(session, user_id=interp.id, booth_id=booth.id, role="interpreter")
        await remove_assignment(session, event_id=event.id, level="booth", membership_id=row.id)
    sent = await _deliveries("role_removed")
    assert [d.recipient for d in sent] == ["interp@example.com"]
    assert "Main Hall · Deutsch at Summit" in await _html_of(sent[0].id)


@pytest.mark.anyio
async def test_team_invitation_links_last_seven_days():
    from datetime import timedelta

    from portal.models import AuthToken, utc_now
    from portal.routers.admin import _send_admin_invite

    async with get_session() as session:
        user = await create_user(session, email="new@example.com", display_name="New")
        await _send_admin_invite(session, user, "interpreter", "Main Hall at Summit", "/workspace/")
        token = await session.scalar(select(AuthToken).where(AuthToken.user_id == user.id))
        expires = token.expires_at if token.expires_at.tzinfo else token.expires_at.replace(tzinfo=utc_now().tzinfo)
    assert expires - utc_now() > timedelta(days=6)
    assert "expires in 7 days" in await _html_of((await _deliveries("role_assignment"))[0].id)


@pytest.mark.anyio
async def test_password_set_change_and_removal_each_send_a_notice():
    from portal.auth import create_user_token, hash_password

    async with get_session() as session:
        user = await create_user(session, email="me@example.com", display_name="Me")
        user_id = user.id
    cookies = {"user_token": create_user_token(user_id=user_id, email="me@example.com")}
    async with _client() as http:
        assert (await http.post("/api/auth/set-password", json={"password": "first-password"}, cookies=cookies)).status_code == 200
        changed = await http.post(
            "/api/auth/set-password", json={"password": "second-password", "current_password": "first-password"}, cookies=cookies
        )
        removed = await http.post("/api/auth/remove-password", json={"password": "second-password"}, cookies=cookies)
    assert changed.status_code == 200 and removed.status_code == 200
    texts = [await _html_of(d.id) for d in await _deliveries("account_access")]
    assert any("You set a password" in t for t in texts)
    assert any("password was changed" in t for t in texts)
    assert any("password was removed" in t for t in texts)
    assert hash_password  # imported helper stays available for other tests


@pytest.mark.anyio
async def test_deleting_an_account_tells_the_person(admin_cookies):
    async with get_session() as session:
        doomed = await create_user(session, email="doomed@example.com", display_name="Doomed")
        doomed_id = doomed.id
    async with _client() as http:
        res = await http.post(
            f"/admin/users/{doomed_id}/delete",
            data={"confirm_email": "doomed@example.com"},
            headers={"origin": "http://test"},
            cookies=admin_cookies,
            follow_redirects=False,
        )
    assert res.status_code == 303
    sent = [d for d in await _deliveries("account_access") if d.recipient == "doomed@example.com"]
    assert len(sent) == 1
    assert "account was deleted" in await _html_of(sent[0].id)


# ── Review fixes ─────────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_subject_is_plain_text_not_html_escaped():
    from portal.crypto import decrypt_val

    async with get_session() as session:
        editor = await create_user(session, email="editor@example.com", display_name="Editor")
        await save_draft(session, "role_assignment", "Welcome to {{ context_name }}", "<p>{{ context_name }}</p>", editor_id=editor.id)
        await publish_draft(session, "role_assignment", editor_id=editor.id)
        delivery = await queue_notification(
            session,
            "role_assignment",
            "a@example.com",
            {"context_name": "Q&A <Summit> O'Neil", "role_name": "Interpreter", "invite_url": "https://example.invalid/i"},
        )
        subject = decrypt_val(delivery.encrypted_subject)
        html = decrypt_val(delivery.encrypted_html)
    assert subject == "Welcome to Q&A <Summit> O'Neil"
    # The body is still HTML-escaped.
    assert "Q&amp;A &lt;Summit&gt;" in html


@pytest.mark.anyio
async def test_email_is_dropped_when_its_token_is_deleted(monkeypatch):
    from portal.models import AuthToken

    monkeypatch.setattr("portal.notifications.settings.smtp_host", "smtp.example.invalid")
    async with get_session() as session:
        user = await create_user(session, email="person@example.com", display_name="Person")
        token = await create_auth_token(session, user_id=user.id, token_type="magic_link", jti="deleted-token")
        delivery = await queue_notification(
            session, "magic_login", user.email, {"magic_link": "https://example.invalid/m"}, auth_token_id=token.id
        )
        delivery_id = delivery.id
    async with get_session() as session:
        # e.g. "Resend invitation" deletes the old token.
        await session.delete(await session.get(AuthToken, token.id))

    assert await process_due_deliveries() == 1
    async with get_session() as session:
        stored = await session.get(EmailDelivery, delivery_id)
    assert stored.status == "suppressed"


@pytest.mark.anyio
async def test_a_failing_purge_does_not_stop_sending(monkeypatch):
    import asyncio

    import portal.notifications as notifications

    sent = 0

    async def broken_purge():
        raise RuntimeError("purge failed")

    async def deliver(limit=20):
        nonlocal sent
        sent += 1
        if sent == 2:
            raise asyncio.CancelledError
        return 0

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(notifications, "purge_old_content", broken_purge)
    monkeypatch.setattr(notifications, "process_due_deliveries", deliver)
    monkeypatch.setattr(notifications.asyncio, "sleep", no_sleep)
    with pytest.raises(asyncio.CancelledError):
        await notifications.notification_worker_loop()
    # Deliveries ran on both loops even though the purge failed.
    assert sent == 2
