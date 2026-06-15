"""Tests for user registration, login, account, and admin user management."""

from __future__ import annotations

import os

os.environ['BOOTH_ACCESS_TOKEN'] = ''
os.environ['ADMIN_PASSWORD'] = 'test-admin-pass'

import pytest

from portal.auth import create_admin_token, create_user_token, hash_password, verify_password

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
async def setup_db():
    from portal.database import configure, dispose, init_db
    configure('sqlite+aiosqlite://')
    await init_db()
    yield
    await dispose()


@pytest.fixture
def admin_cookie():
    return {'admin_token': create_admin_token()}


def _client():
    from httpx import ASGITransport, AsyncClient

    from fastapi_app import app
    return AsyncClient(transport=ASGITransport(app=app), base_url='http://test')


async def _create_test_user(email='test@example.com', display_name='Test User', password='securepass123'):
    from portal.database import create_user, get_session
    pw_hash = hash_password(password)
    async with get_session() as s:
        user = await create_user(s, email=email, display_name=display_name, password_hash=pw_hash)
    return user


# ---------------------------------------------------------------------------
# Password hashing
# ---------------------------------------------------------------------------


class TestPasswordHashing:
    @pytest.mark.anyio
    async def test_hash_and_verify(self, setup_db):
        pw = 'my-secret-password'
        hashed = hash_password(pw)
        assert hashed != pw
        assert verify_password(pw, hashed)

    @pytest.mark.anyio
    async def test_wrong_password_fails(self, setup_db):
        hashed = hash_password('correct')
        assert not verify_password('wrong', hashed)


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


class TestRegistration:
    @pytest.mark.anyio
    async def test_register_page_renders(self, setup_db):
        async with _client() as c:
            resp = await c.get('/register')
        assert resp.status_code == 200
        assert b'Create your account' in resp.content

    @pytest.mark.anyio
    async def test_register_creates_listener_account(self, setup_db):
        async with _client() as c:
            resp = await c.post('/register', data={
                'email': 'new@example.com',
                'display_name': 'New User',
                'password': 'securepass123',
                'password_confirm': 'securepass123',
            }, follow_redirects=False)
        assert resp.status_code == 303
        assert resp.headers['location'] == '/account'
        assert 'user_token' in resp.headers.get('set-cookie', '')

    @pytest.mark.anyio
    async def test_register_rejects_short_password(self, setup_db):
        async with _client() as c:
            resp = await c.post('/register', data={
                'email': 'new@example.com',
                'display_name': 'New User',
                'password': 'short',
                'password_confirm': 'short',
            })
        assert resp.status_code == 422
        assert b'at least 8 characters' in resp.content

    @pytest.mark.anyio
    async def test_register_rejects_mismatched_passwords(self, setup_db):
        async with _client() as c:
            resp = await c.post('/register', data={
                'email': 'new@example.com',
                'display_name': 'New User',
                'password': 'securepass123',
                'password_confirm': 'differentpass',
            })
        assert resp.status_code == 422
        assert b'do not match' in resp.content

    @pytest.mark.anyio
    async def test_register_rejects_duplicate_email(self, setup_db):
        await _create_test_user(email='dupe@example.com')
        async with _client() as c:
            resp = await c.post('/register', data={
                'email': 'dupe@example.com',
                'display_name': 'Another User',
                'password': 'securepass123',
                'password_confirm': 'securepass123',
            })
        assert resp.status_code == 422
        assert b'already exists' in resp.content

    @pytest.mark.anyio
    async def test_register_creates_active_non_admin(self, setup_db):
        from portal.database import get_session, get_user_by_email
        async with _client() as c:
            await c.post('/register', data={
                'email': 'listener@example.com',
                'display_name': 'Listener',
                'password': 'securepass123',
                'password_confirm': 'securepass123',
            }, follow_redirects=False)
        async with get_session() as s:
            user = await get_user_by_email(s, 'listener@example.com')
        assert user is not None
        assert user.is_admin is False
        assert user.is_active is True


# ---------------------------------------------------------------------------
# Login
# ---------------------------------------------------------------------------


class TestUserLogin:
    @pytest.mark.anyio
    async def test_login_page_renders(self, setup_db):
        async with _client() as c:
            resp = await c.get('/login')
        assert resp.status_code == 200
        assert b'Sign in' in resp.content

    @pytest.mark.anyio
    async def test_login_with_correct_credentials(self, setup_db):
        await _create_test_user(email='login@example.com', password='securepass123')
        async with _client() as c:
            resp = await c.post('/login', data={
                'email': 'login@example.com',
                'password': 'securepass123',
            }, follow_redirects=False)
        assert resp.status_code == 303
        assert resp.headers['location'] == '/account'
        assert 'user_token' in resp.headers.get('set-cookie', '')

    @pytest.mark.anyio
    async def test_login_with_wrong_password(self, setup_db):
        await _create_test_user(email='login@example.com', password='securepass123')
        async with _client() as c:
            resp = await c.post('/login', data={
                'email': 'login@example.com',
                'password': 'wrongpassword',
            })
        assert resp.status_code == 403
        assert b'Invalid email or password' in resp.content

    @pytest.mark.anyio
    async def test_login_with_nonexistent_email(self, setup_db):
        async with _client() as c:
            resp = await c.post('/login', data={
                'email': 'nobody@example.com',
                'password': 'whatever',
            })
        assert resp.status_code == 403

    @pytest.mark.anyio
    async def test_login_deactivated_user_rejected(self, setup_db):
        from portal.database import get_session, update_user_active
        user = await _create_test_user(email='deactivated@example.com', password='securepass123')
        async with get_session() as s:
            await update_user_active(s, user.id, is_active=False)
        async with _client() as c:
            resp = await c.post('/login', data={
                'email': 'deactivated@example.com',
                'password': 'securepass123',
            })
        assert resp.status_code == 403
        assert b'deactivated' in resp.content


# ---------------------------------------------------------------------------
# Logout
# ---------------------------------------------------------------------------


class TestUserLogout:
    @pytest.mark.anyio
    async def test_logout_clears_cookie(self, setup_db):
        async with _client() as c:
            resp = await c.get('/logout', follow_redirects=False)
        assert resp.status_code == 303
        assert resp.headers['location'] == '/'


# ---------------------------------------------------------------------------
# Account page
# ---------------------------------------------------------------------------


class TestAccountPage:
    @pytest.mark.anyio
    async def test_account_requires_login(self, setup_db):
        async with _client() as c:
            resp = await c.get('/account', follow_redirects=False)
        assert resp.status_code == 303
        assert resp.headers['location'] == '/login'

    @pytest.mark.anyio
    async def test_account_shows_user_info(self, setup_db):
        user = await _create_test_user(email='me@example.com', display_name='Me')
        token = create_user_token(user_id=user.id, email=user.email, is_admin=user.is_admin)
        async with _client() as c:
            resp = await c.get('/account', cookies={'user_token': token})
        assert resp.status_code == 200
        assert b'me@example.com' in resp.content
        assert b'Me' in resp.content


# ---------------------------------------------------------------------------
# Admin user management
# ---------------------------------------------------------------------------


class TestAdminUserManagement:
    @pytest.mark.anyio
    async def test_user_list_shows_users(self, setup_db, admin_cookie):
        await _create_test_user(email='user1@example.com', display_name='User One')
        async with _client() as c:
            resp = await c.get('/admin/users/', cookies=admin_cookie)
        assert resp.status_code == 200
        assert b'user1@example.com' in resp.content
        assert b'User One' in resp.content

    @pytest.mark.anyio
    async def test_user_list_requires_admin(self, setup_db):
        async with _client() as c:
            resp = await c.get('/admin/users/')
        assert resp.status_code == 403

    @pytest.mark.anyio
    async def test_set_event_membership(self, setup_db, admin_cookie):
        from portal.database import create_event, get_session, list_memberships_for_event
        user = await _create_test_user()
        async with get_session() as s:
            event = await create_event(s, display_name='Test Event', slug='test-event')
        async with _client() as c:
            resp = await c.post(
                f'/admin/events/{event.id}/members/',
                data={'email': user.email, 'role': 'interpreter'},
                cookies=admin_cookie,
                follow_redirects=False,
            )
        assert resp.status_code == 303
        async with get_session() as s:
            memberships = await list_memberships_for_event(s, event.id)
        assert len(memberships) == 1
        assert memberships[0].role == 'interpreter'

    @pytest.mark.anyio
    async def test_toggle_user_active(self, setup_db, admin_cookie):
        from portal.database import get_session, get_user_by_id
        user = await _create_test_user()
        assert user.is_active is True
        async with _client() as c:
            resp = await c.post(
                f'/admin/users/{user.id}/toggle-active',
                cookies=admin_cookie,
                follow_redirects=False,
            )
        assert resp.status_code == 303
        async with get_session() as s:
            updated = await get_user_by_id(s, user.id)
        assert updated.is_active is False

    @pytest.mark.anyio
    async def test_delete_user(self, setup_db, admin_cookie):
        from portal.database import get_session, get_user_by_id
        user = await _create_test_user()
        async with _client() as c:
            resp = await c.post(
                f'/admin/users/{user.id}/delete',
                cookies=admin_cookie,
                follow_redirects=False,
            )
        assert resp.status_code == 303
        async with get_session() as s:
            deleted = await get_user_by_id(s, user.id)
        assert deleted is None


# ---------------------------------------------------------------------------
# Home page auth links
# ---------------------------------------------------------------------------


class TestHomePageAuthLinks:
    @pytest.mark.anyio
    async def test_home_shows_login_register_when_logged_out(self, setup_db):
        async with _client() as c:
            resp = await c.get('/')
        assert resp.status_code == 200
        assert b'Sign In' in resp.content
        assert b'Register' in resp.content

    @pytest.mark.anyio
    async def test_home_shows_account_when_logged_in(self, setup_db):
        user = await _create_test_user()
        token = create_user_token(user_id=user.id, email=user.email, is_admin=user.is_admin)
        async with _client() as c:
            resp = await c.get('/', cookies={'user_token': token})
        assert resp.status_code == 200
        assert b'Logout' in resp.content
