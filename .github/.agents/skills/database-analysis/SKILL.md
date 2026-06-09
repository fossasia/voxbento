# Skill: Database Analysis

> Use this skill to analyse, audit, or modify database models, migrations, and CRUD helpers.
> Reference: `portal/models.py`, `portal/database.py`, `alembic/versions/`.

---

## Quick Schema Reference

```
events ─────────┬─── rooms ───── booths ─── invite_tokens
                │                  │
                │                  └─── booth_memberships ─── users
                └─── booths ───────────── event_memberships ─ users
```

See [DATABASE_MAP.md](../../context/DATABASE_MAP.md) for full column details.

---

## Key Design Decisions

1. **`mediamtx_path` is a runtime property**, not a stored column on `DBBooth`.
   - Always derived via `make_mediamtx_path(event.slug, language_code)`.
   - Requires `event` relationship to be loaded: use `joinedload(DBBooth.event)`.

2. **API keys are Fernet-encrypted** in the `events` table.
   - Use `portal.crypto.encrypt_val(key)` to store.
   - Use `portal.crypto.decrypt_val(encrypted)` to read.
   - `API_KEY_ENCRYPTION_KEY` env var must be set; raises `RuntimeError` if default.

3. **Unique index on `(event_id, language_code)`** in `booths` — one booth per language per event.

4. **`InviteToken.token` is a 64-char hex string** (32 bytes of entropy via `secrets.token_hex(32)`).

5. **Session pattern uses `async with session.begin()`** — auto-commits and auto-rolls back.
   ```python
   async with get_session() as session:
       result = await create_event(session, slug='test', display_name='Test')
       # committed on exit
   ```

---

## Working with Relationships

### Loading `event` on `DBBooth` (for `mediamtx_path`)
```python
from sqlalchemy.orm import joinedload
stmt = select(DBBooth).options(joinedload(DBBooth.event)).where(DBBooth.id == booth_id)
```

### Loading booth with room and event
```python
stmt = (
    select(DBBooth)
    .join(Event)
    .options(joinedload(DBBooth.room))
    .where(Event.slug == event_slug)
    .where(DBBooth.language_code == language_code)
)
```

### Invite token with booth and event (for redemption)
```python
result = await session.execute(
    select(InviteToken)
    .options(joinedload(InviteToken.booth).joinedload(DBBooth.event))
    .where(InviteToken.token == token_str)
)
```

---

## Migration Workflow

1. Edit `portal/models.py`.
2. Generate migration:
```bash
uv run alembic revision --autogenerate -m "short_description"
```
3. **Review generated file** — autogenerate is not always correct:
   - SQLite requires `batch_alter_table` for column additions/drops. See migration 008 as the canonical example.
   - For new tables, autogenerate is usually correct.
4. Apply:
```bash
uv run alembic upgrade head
```
5. Test:
```bash
uv run pytest tests/test_database.py -v
```

### Migration 008 Reference Pattern (SQLite column add)
```python
def upgrade() -> None:
    with op.batch_alter_table('booths') as batch_op:
        batch_op.add_column(sa.Column('new_column', sa.String(20), server_default=sa.text("'default'"), nullable=False))
```

---

## CRUD Helper Patterns

### Upsert pattern (event/booth memberships)
`set_event_membership` and `set_booth_membership` use an upsert-like pattern:
- Query for existing membership → update role if found → create new if not found.

### Invite token redemption
`redeem_invite_token` raises `ValueError` (not HTTP exception) on invalid token:
```python
tok = await redeem_invite_token(session, token_str)
# Raises ValueError: "Token has already been used." or "Token has expired."
# Returns None if token not found
```
The route handler converts these to HTTP 403 / 404.

### Revoke token
`revoke_invite_token` sets `used_at = utc_now()` on the token (same effect as redemption).

---

## Testing Database Code

Use in-memory SQLite + `configure()` override in test fixtures:
```python
from portal.database import configure, init_db, drop_db
from portal.config import settings

async def setup_test_db():
    configure('sqlite+aiosqlite:///:memory:')
    await init_db()

async def teardown_test_db():
    await drop_db()
```

Never use the production database URL in tests.

---

## Common Database Issues

| Issue | Cause | Fix |
|---|---|---|
| `mediamtx_path` raises `AttributeError` | `event` relationship not loaded | Use `joinedload(DBBooth.event)` in query |
| `IntegrityError` on booth create | Duplicate `(event_id, language_code)` | Catch `IntegrityError` and return 409 |
| Migration fails on SQLite | Column alter without `batch_alter_table` | Rewrite migration with batch context |
| `decrypt_val` raises `ValueError` | Wrong `API_KEY_ENCRYPTION_KEY` | Key must match the one used to encrypt |
| `InviteToken.is_expired` incorrect | Missing timezone on `expires_at` | `expires_at` must be timezone-aware (UTC) |
