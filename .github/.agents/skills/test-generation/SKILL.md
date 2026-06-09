# Skill: Test Generation

> Use this skill to write tests for VoxBento features.
> All tests use `pytest` + `anyio`. Reference: `tests/conftest.py`.

---

## Test Infrastructure

### Setup (`tests/conftest.py`)
```python
pytest_plugins = ('anyio',)

@pytest.fixture(params=['asyncio'])
def anyio_backend(request):
    return request.param
```

All async tests use `@pytest.mark.anyio` or `@pytest.mark.asyncio`.

### In-memory DB fixture pattern
```python
import pytest
from portal.database import configure, init_db, drop_db, get_session

@pytest.fixture(autouse=True)
async def test_db():
    configure('sqlite+aiosqlite:///:memory:')
    await init_db()
    yield
    await drop_db()
```

### FastAPI test client pattern
```python
import pytest
from httpx import AsyncClient, ASGITransport
from fastapi_app import app

@pytest.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as c:
        yield c
```

---

## Test Patterns by Area

### Booth State Tests (`tests/test_booth_state.py`)
```python
from portal.booth_state import BoothRegistry

async def test_join_participant():
    registry = BoothRegistry()
    participant, state = await registry.join_participant(
        booth_id='test-en',
        display_name='Alice',
        role='interpreter',
        language='English',
        channel_id='test/en',
    )
    assert participant.role == 'interpreter'
    assert state['active_interpreter_id'] == participant.participant_id
```

### Database Tests (`tests/test_database.py`)
```python
from portal.database import create_event, create_room, create_booth, get_session

async def test_create_event(test_db):
    async with get_session() as session:
        ev = await create_event(session, slug='myevent', display_name='My Event')
    assert ev.id is not None
    assert ev.slug == 'myevent'
```

### Route Tests (`tests/test_fastapi_app.py`)
```python
async def test_healthz(client):
    resp = await client.get('/healthz')
    assert resp.status_code == 200
    data = resp.json()
    assert data['ok'] is True

async def test_login_invalid(client):
    resp = await client.post('/login', data={'email': 'x@x.com', 'password': 'wrong'})
    assert resp.status_code == 403
```

### Auth Tests (`tests/test_user_auth.py`)
```python
from portal.auth import create_user_token, decode_token

def test_user_token_roundtrip():
    token = create_user_token(user_id=1, email='a@b.com', display_name='Alice', is_admin=False)
    payload = decode_token(token)
    assert payload['sub'] == '1'
    assert payload['user'] is True
    assert payload['is_admin'] is False
```

### Invite Token Tests (`tests/test_join_flow.py`)
```python
async def test_invite_token_join(client, test_db):
    # Setup: create event, room, booth, token via DB helpers
    async with get_session() as session:
        ev = await create_event(session, slug='test', display_name='T')
        room = await create_room(session, event_id=ev.id, display_name='R')
        booth = await create_booth(session, event_id=ev.id, room_id=room.id,
                                   language_code='en', language_name='English')
        token = await create_invite_token(session, booth_id=booth.id, role='interpreter')
    
    resp = await client.get(f'/join/{token.token}', follow_redirects=False)
    assert resp.status_code == 303
    assert '/interpreter/test/en' in resp.headers['location']
    assert 'session_token' in resp.cookies
```

### WebSocket Tests
```python
async def test_ws_booth_join(client, test_db):
    # Setup booth + user with role
    ...
    async with client.websocket_connect(f'/ws/booth/test-en') as ws:
        await ws.send_json({
            'type': 'booth:join',
            'display_name': 'Alice',
            'role': 'interpreter',
            'language': 'English',
            'channel_id': 'test/en',
        })
        msg = await ws.receive_json()
        assert msg['type'] == 'booth:joined'
```

---

## Test File Conventions

| Test area | File |
|---|---|
| Route/HTTP | `tests/test_fastapi_app.py` |
| Booth state logic | `tests/test_booth_state.py` |
| Booth identity scheme | `tests/test_booth_identity.py` |
| DB CRUD | `tests/test_database.py` |
| Admin panel | `tests/test_admin_panel.py` |
| Roles + permissions | `tests/test_roles.py` |
| Crypto | `tests/test_crypto.py` |
| User auth | `tests/test_user_auth.py` |
| Invite token join | `tests/test_join_flow.py` |
| Memberships | `tests/test_memberships_tokens.py` |
| Transcription | `tests/test_transcription_concurrency.py` |

---

## Coverage Gaps (prioritised)

1. WS token scope mismatch → 4003 close code.
2. `CaptionAggregator` forced finalization (50 words, 15 seconds).
3. Fernet key rotation (MultiFernet with 2 keys).
4. `BoothRegistry.set_active_interpreter` permission denial.
5. Admin route 403 when no auth cookie.
6. `_ensure_mediamtx_path` cache invalidation path.
7. `redeem_invite_token` → already used + expired cases.
