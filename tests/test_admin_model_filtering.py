"""Regression tests for admin transcription provider-model dropdown filtering (#905).

Covers:
- Template markup in room_detail.html and booth_detail.html (data-provider-filter, data-model-filter).
- Removal of duplicated inline JavaScript filter scripts.
- Single inclusion of admin.js (inherited from admin/base.html, removed duplicate in booth_detail.html).
- Preservation of option attributes, names, IDs, and form submission behavior.
- Automated JavaScript unit testing of admin.js provider-model filtering via Node.js.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from html.parser import HTMLParser

import pytest
from httpx import ASGITransport, AsyncClient

from fastapi_app import app
from portal.auth import create_user_token, hash_password
from portal.database import (
    configure,
    create_booth,
    create_event,
    create_room,
    create_user,
    dispose,
    get_booth_by_id,
    get_room_by_id,
    get_session,
    init_db,
)

os.environ["BOOTH_ACCESS_TOKEN"] = ""


def _extract_inline_scripts(html: str) -> list[str]:
    """Extract contents of all inline <script> tags without src attribute."""
    scripts: list[str] = []
    in_inline = False
    data_acc: list[str] = []

    class Parser(HTMLParser):
        def handle_starttag(self, tag, attrs):
            nonlocal in_inline, data_acc
            if tag.lower() == "script":
                attr_dict = dict(attrs)
                if "src" not in attr_dict:
                    in_inline = True
                    data_acc = []

        def handle_endtag(self, tag):
            nonlocal in_inline, data_acc
            if tag.lower() == "script" and in_inline:
                scripts.append("".join(data_acc))
                in_inline = False
                data_acc = []

        def handle_data(self, data):
            if in_inline:
                data_acc.append(data)

    Parser().feed(html)
    return scripts


@pytest.fixture(autouse=True)
async def setup_db():
    configure("sqlite+aiosqlite://")
    await init_db()
    yield
    await dispose()


@pytest.fixture
async def seeded_admin():
    async with get_session() as s:
        user = await create_user(
            s,
            email="admin@example.com",
            display_name="Admin",
            password_hash=hash_password("test-admin-pass"),
            is_admin=True,
            email_verified=True,
        )
    return user


@pytest.fixture
async def admin_client(seeded_admin):
    token = create_user_token(user_id=seeded_admin.id, email=seeded_admin.email, is_admin=True)
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={"user_token": token},
    ) as client:
        yield client


@pytest.fixture
async def seed_event_room_booth():
    async with get_session() as s:
        event = await create_event(s, slug="testcon", display_name="TestCon 2026")
        event.transcription_api_enabled = True
        event.encrypted_openai_api_key = b"enc_key"
        event.encrypted_deepgram_api_key = b"enc_key"
        event.encrypted_elevenlabs_api_key = b"enc_key"
        room = await create_room(s, event_id=event.id, display_name="Main Hall")
        room.floor_transcription_enabled = True
        booth = await create_booth(
            s,
            event_id=event.id,
            room_id=room.id,
            language_code="en",
            language_name="English",
        )
    return event, room, booth


@pytest.mark.anyio
async def test_room_detail_template_provider_model_filtering(admin_client, seed_event_room_booth):
    """Room detail template must have data attributes and no inline filter script."""
    event, room, _ = seed_event_room_booth

    resp = await admin_client.get(f"/admin/events/{event.id}/rooms/{room.id}/")
    assert resp.status_code == 200
    html = resp.text

    # Provider select has data-provider-filter pointing to floor_transcription_model
    assert re.search(
        r'<select[^>]*id="floor_transcription_provider"[^>]*data-provider-filter="floor_transcription_model"',
        html,
    ), "floor_transcription_provider must declare data-provider-filter='floor_transcription_model'"

    # Model select has data-model-filter pointing to floor_transcription_provider
    assert re.search(
        r'<select[^>]*id="floor_transcription_model"[^>]*data-model-filter="floor_transcription_provider"',
        html,
    ), "floor_transcription_model must declare data-model-filter='floor_transcription_provider'"

    # Available speech provider options are present
    for provider in ("local", "openai", "deepgram", "elevenlabs"):
        assert f'value="{provider}"' in html

    # Model options with data-provider attributes are preserved
    for model, provider in [
        ("tiny", "local"),
        ("base", "local"),
        ("gpt-realtime-whisper", "openai"),
        ("gpt-4o-transcribe", "openai"),
        ("gpt-4o-mini-transcribe", "openai"),
        ("whisper-1", "openai"),
        ("nova-2", "deepgram"),
        ("parakeet-rnnt", "nvidia"),
        ("parakeet-ctc", "nvidia"),
        ("scribe_v2_realtime", "elevenlabs"),
    ]:
        assert f'value="{model}" data-provider="{provider}"' in html

    # Ensure room-detail.js is included for room page and admin.js is included once via admin/base.html
    assert html.count("js/room-detail.js") == 1
    assert html.count("js/admin.js") == 1

    # Verify no inline script blocks contain floor transcription filtering logic
    for s in _extract_inline_scripts(html):
        assert "floor_transcription_provider" not in s
        assert "floor_transcription_model" not in s
        assert "filterModels" not in s

    # Verify portal/static/js/room-detail.js imports and uses the centralized helper without duplicating filterModels
    room_detail_js_path = os.path.join(
        os.path.dirname(os.path.dirname(__file__)),
        "portal",
        "static",
        "js",
        "room-detail.js",
    )
    with open(room_detail_js_path, "r", encoding="utf-8") as f:
        room_js = f.read()

    assert "import { initProviderModelFiltering } from './admin.js';" not in room_js
    assert "window.initProviderModelFiltering" in room_js
    assert "function filterModels" not in room_js, "room-detail.js must not define a duplicate filterModels function"


@pytest.mark.anyio
async def test_booth_detail_template_provider_model_filtering(admin_client, seed_event_room_booth):
    """Booth detail template must have data attributes, no duplicate admin.js, and no inline script."""
    event, room, booth = seed_event_room_booth

    resp = await admin_client.get(f"/admin/events/{event.id}/rooms/{room.id}/booths/{booth.id}/")
    assert resp.status_code == 200
    html = resp.text

    # Provider select has data-provider-filter pointing to transcription_model
    assert re.search(
        r'<select[^>]*id="transcription_provider"[^>]*data-provider-filter="transcription_model"',
        html,
    ), "transcription_provider must declare data-provider-filter='transcription_model'"

    # Model select has data-model-filter pointing to transcription_provider
    assert re.search(
        r'<select[^>]*id="transcription_model"[^>]*data-model-filter="transcription_provider"',
        html,
    ), "transcription_model must declare data-model-filter='transcription_provider'"

    # Duplicated inline filtering script for transcription_provider is removed
    assert "const providerSelect = document.getElementById('transcription_provider');" not in html
    for s in _extract_inline_scripts(html):
        assert "transcription_provider" not in s
        assert "filterModels" not in s

    # admin.js must be included exactly once (inherited from admin/base.html, removed duplicate in booth_detail.html)
    assert html.count("js/admin.js") == 1


@pytest.mark.anyio
async def test_room_transcription_form_submission_preserves_values(admin_client, seed_event_room_booth):
    """Form submission for room transcription settings saves provider and model correctly."""
    event, room, _ = seed_event_room_booth

    resp = await admin_client.post(
        f"/admin/events/{event.id}/rooms/{room.id}/edit",
        data={
            "form_section": "transcription",
            "floor_transcription_enabled": "on",
            "floor_language_code": "en",
            "floor_transcription_provider": "openai",
            "floor_transcription_model": "gpt-4o-transcribe",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303

    async with get_session() as session:
        updated = await get_room_by_id(session, room.id)
        assert updated.floor_transcription_provider == "openai"
        assert updated.floor_transcription_model == "gpt-4o-transcribe"


@pytest.mark.anyio
async def test_booth_transcription_form_submission_preserves_values(admin_client, seed_event_room_booth):
    """Form submission for booth transcription settings saves provider and model correctly."""
    event, room, booth = seed_event_room_booth

    resp = await admin_client.post(
        f"/admin/events/{event.id}/rooms/{room.id}/booths/{booth.id}/transcription-settings",
        data={
            "transcription_enabled": "true",
            "transcription_provider": "local",
            "transcription_model": "base",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303

    async with get_session() as session:
        updated = await get_booth_by_id(session, booth.id)
        assert updated.transcription_provider == "local"
        assert updated.transcription_model == "base"


@pytest.mark.anyio
async def test_admin_js_filtering_unit_tests():
    """Execute unit test suite against portal/static/js/admin.js in Node.js to verify client-side filtering."""
    node_bin = shutil.which("node")
    if not node_bin:
        pytest.skip("Node.js is not installed on this system")

    js_test_code = """
globalThis.window = globalThis;
globalThis.document = {
  readyState: 'complete',
  body: { dataset: {} },
  getElementById: (id) => globalThis.elements?.[id] || null,
  querySelectorAll: (selector) => {
    const els = Object.values(globalThis.elements || {});
    if (selector === 'select[data-provider-filter]') {
      return els.filter(el => el.dataset && el.dataset.providerFilter);
    }
    if (selector === 'select[data-model-filter]') {
      return els.filter(el => el.dataset && el.dataset.modelFilter);
    }
    return [];
  },
  addEventListener: () => {}
};

class MockOption {
  constructor(value, text, provider, selected = false) {
    this.value = value;
    this.textContent = text;
    this.dataset = provider ? { provider } : {};
    this.attrs = new Map();
    if (provider) this.attrs.set('data-provider', provider);
    this._selected = selected;
    this.disabled = false;
    this.hidden = false;
    this.style = {};
    this.select = null;
  }
  get selected() { return this._selected; }
  set selected(val) {
    this._selected = val;
    if (val && this.select) {
      for (const opt of this.select.options) {
        if (opt !== this) opt._selected = false;
      }
    }
  }
  getAttribute(name) { return this.attrs.get(name) || null; }
  setAttribute(name, val) { this.attrs.set(name, val); }
}

class MockSelect {
  constructor(id, options = [], value = '') {
    this.id = id;
    this.options = options;
    for (const opt of options) { opt.select = this; }
    this.dataset = {};
    this.listeners = {};
    if (value) { this.value = value; }
  }
  get value() {
    const sel = this.options.find(o => o.selected);
    return sel ? sel.value : '';
  }
  set value(v) {
    let matched = false;
    this.options.forEach(o => {
      if (o.value === v) {
        o.selected = true;
        matched = true;
      } else {
        o.selected = false;
      }
    });
    if (!matched) {
      this.options.forEach(o => { o.selected = false; });
    }
  }
  get selectedIndex() {
    return this.options.findIndex(o => o.selected);
  }
  set selectedIndex(idx) {
    this.options.forEach((o, i) => { o.selected = (i === idx); });
  }
  addEventListener(event, fn) {
    this.listeners[event] = this.listeners[event] || [];
    this.listeners[event].push(fn);
  }
  dispatchEvent(event) {
    (this.listeners[event.type] || []).forEach(fn => fn(event));
  }
  triggerChange(newVal) {
    this.value = newVal;
    this.dispatchEvent({ type: 'change' });
  }
}

await import('./portal/static/js/admin.js');
const { initProviderModelPair, initProviderModelFiltering } = globalThis;

// 1. Initial filtering on load
const providerSelect = new MockSelect('provider', [
  new MockOption('local', 'Local', null),
  new MockOption('openai', 'OpenAI', null),
  new MockOption('deepgram', 'Deepgram', null),
], 'local');

const modelSelect = new MockSelect('model', [
  new MockOption('tiny', 'tiny', 'local', true),
  new MockOption('base', 'base', 'local'),
  new MockOption('whisper-1', 'whisper-1', 'openai'),
  new MockOption('nova-2', 'nova-2', 'deepgram'),
]);

initProviderModelPair(providerSelect, modelSelect);
if (modelSelect.options[0].style.display !== '' || modelSelect.options[0].disabled !== false) {
  throw new Error('Local tiny option should be visible and enabled');
}
if (modelSelect.options[2].style.display !== 'none' || modelSelect.options[2].disabled !== true) {
  throw new Error('OpenAI whisper option should be hidden and disabled when local is selected');
}
if (modelSelect.value !== 'tiny') {
  throw new Error('Initial model selection should be tiny');
}

// 2. Switching provider updates available models and selection
providerSelect.triggerChange('openai');
if (modelSelect.options[0].style.display !== 'none' || modelSelect.options[0].disabled !== true) {
  throw new Error('Local tiny option should be hidden when switched to openai');
}
if (modelSelect.options[2].style.display !== '' || modelSelect.options[2].disabled !== false) {
  throw new Error('OpenAI whisper-1 should be visible when switched to openai');
}
if (modelSelect.value !== 'whisper-1') {
  throw new Error('whisper-1 should be selected after switching to openai');
}

// 3. Repeated switches do not duplicate or corrupt options
providerSelect.triggerChange('deepgram');
if (modelSelect.value !== 'nova-2') throw new Error('nova-2 should be selected for deepgram');
providerSelect.triggerChange('openai');
if (modelSelect.value !== 'whisper-1') throw new Error('whisper-1 should be selected for openai');
providerSelect.triggerChange('local');
if (modelSelect.value !== 'tiny') throw new Error('tiny should be selected for local');
if (modelSelect.options.length !== 4) throw new Error('Options count should remain 4 without duplication');

// 4. Shared options across providers
const sharedModel = new MockSelect('shared_model', [
  new MockOption('tiny', 'tiny', 'local'),
  new MockOption('shared', 'shared', 'local,openai'),
  new MockOption('all_star', 'all_star', '*'),
  new MockOption('all_word', 'all_word', 'all'),
]);
const sharedProvider = new MockSelect('shared_provider', [
  new MockOption('local', 'Local', null),
  new MockOption('openai', 'OpenAI', null),
], 'local');
sharedModel.options[1].selected = true; // select 'shared'
initProviderModelPair(sharedProvider, sharedModel);
if (sharedModel.value !== 'shared') throw new Error('Shared option should be initially selected');
sharedProvider.triggerChange('openai');
if (sharedModel.value !== 'shared') throw new Error('Shared option should stay selected across compatible providers');
if (sharedModel.options[2].style.display !== '') throw new Error('Wildcard * option should be visible');
if (sharedModel.options[3].style.display !== '') throw new Error('Wildcard all option should be visible');

// 5. Empty provider selection
providerSelect.triggerChange('');
if (modelSelect.options.some(o => o.style.display !== 'none')) {
  throw new Error('All provider-specific options should be hidden when provider is empty');
}

// 5b. Incompatible selection reset when zero models are compatible (clearing selection)
const onlyLocalModel = new MockSelect('only_local', [
  new MockOption('tiny', 'tiny', 'local', true),
]);
const onlyLocalProvider = new MockSelect('p_only', [
  new MockOption('local', 'Local'),
  new MockOption('none', 'None'),
], 'local');
initProviderModelPair(onlyLocalProvider, onlyLocalModel);
onlyLocalProvider.triggerChange('none');
if (onlyLocalModel.value !== '' || onlyLocalModel.selectedIndex !== -1) {
  throw new Error('Model selection should be cleared when no options are compatible');
}

// 6. Graceful handling of null/missing elements
initProviderModelPair(null, null);
initProviderModelPair(providerSelect, null);
initProviderModelPair(null, modelSelect);

// 7. initProviderModelFiltering with data attributes and duplicate listener prevention
const pEl = new MockSelect('p1', [new MockOption('local', 'Local'), new MockOption('openai', 'OpenAI')], 'openai');
pEl.dataset.providerFilter = 'm1';
const mEl = new MockSelect('m1', [new MockOption('tiny', 'tiny', 'local'), new MockOption('w1', 'w1', 'openai')]);
mEl.dataset.modelFilter = 'p1';
globalThis.elements = { p1: pEl, m1: mEl };

initProviderModelFiltering();
if (mEl.value !== 'w1') throw new Error('mEl should have w1 selected via data attributes');

// Calling initProviderModelFiltering again should be idempotent
initProviderModelFiltering();
pEl.triggerChange('local');
if (mEl.value !== 'tiny') throw new Error('mEl should switch to tiny without double-listener side effects');

// 8. Verify helpers exported to window
if (typeof globalThis.window.initProviderModelFiltering !== 'function') {
  throw new Error('window.initProviderModelFiltering should be defined');
}
if (typeof globalThis.window.initProviderModelPair !== 'function') {
  throw new Error('window.initProviderModelPair should be defined');
}

console.log('OK');
"""

    result = subprocess.run(
        [node_bin, "--input-type=module", "-e", js_test_code],
        cwd=os.path.dirname(os.path.dirname(__file__)),
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, f"Node JS test failed:\nstdout: {result.stdout}\nstderr: {result.stderr}"
    assert "OK" in result.stdout


@pytest.mark.anyio
async def test_room_detail_js_filtering_unit_test():
    """Verify portal/static/js/room-detail.js initializes filtering via imported admin.js helper."""
    node_bin = shutil.which("node")
    if not node_bin:
        pytest.skip("Node.js is not installed on this system")

    js_code = """
globalThis.window = globalThis;
globalThis.history = { replaceState: () => {} };
globalThis.sessionStorage = { getItem: () => '', setItem: () => {} };

class MockOption {
  constructor(value, text, provider, selected = false) {
    this.value = value;
    this.textContent = text;
    this.dataset = provider ? { provider } : {};
    this.attrs = new Map();
    if (provider) this.attrs.set('data-provider', provider);
    this._selected = selected;
    this.disabled = false;
    this.hidden = false;
    this.style = {};
    this.select = null;
  }
  get selected() { return this._selected; }
  set selected(val) {
    this._selected = val;
    if (val && this.select) {
      for (const opt of this.select.options) {
        if (opt !== this) opt._selected = false;
      }
    }
  }
  getAttribute(name) { return this.attrs.get(name) || null; }
  setAttribute(name, val) { this.attrs.set(name, val); }
}

class MockSelect {
  constructor(id, options = [], value = '') {
    this.id = id;
    this.options = options;
    for (const opt of options) { opt.select = this; }
    this.dataset = {};
    this.listeners = {};
    if (value) { this.value = value; }
  }
  get value() {
    const sel = this.options.find(o => o.selected);
    return sel ? sel.value : '';
  }
  set value(v) {
    let matched = false;
    this.options.forEach(o => {
      if (o.value === v) {
        o.selected = true;
        matched = true;
      } else {
        o.selected = false;
      }
    });
    if (!matched) {
      this.options.forEach(o => { o.selected = false; });
    }
  }
  get selectedIndex() {
    return this.options.findIndex(o => o.selected);
  }
  set selectedIndex(idx) {
    this.options.forEach((o, i) => { o.selected = (i === idx); });
  }
  addEventListener(event, fn) {
    this.listeners[event] = this.listeners[event] || [];
    this.listeners[event].push(fn);
  }
  dispatchEvent(event) {
    (this.listeners[event.type] || []).forEach(fn => fn(event));
  }
  triggerChange(newVal) {
    this.value = newVal;
    this.dispatchEvent({ type: 'change' });
  }
}

const provider = new MockSelect('floor_transcription_provider', [
  new MockOption('local', 'Local'),
  new MockOption('openai', 'OpenAI')
], 'local');
provider.dataset.providerFilter = 'floor_transcription_model';

const model = new MockSelect('floor_transcription_model', [
  new MockOption('tiny', 'tiny', 'local', true),
  new MockOption('whisper-1', 'whisper-1', 'openai')
]);
model.dataset.modelFilter = 'floor_transcription_provider';

const rootEl = { dataset: { roomId: '42', translationLanguages: '[]', selectedTranslationLanguages: '[]' } };

globalThis.elements = {
  'room-detail': rootEl,
  'floor_transcription_provider': provider,
  'floor_transcription_model': model,
};

globalThis.document = {
  readyState: 'complete',
  body: { dataset: {} },
  getElementById: (id) => globalThis.elements[id] || null,
  querySelector: () => null,
  querySelectorAll: (selector) => {
    const els = Object.values(globalThis.elements || {});
    if (selector === 'select[data-provider-filter]') {
      return els.filter(el => el.dataset && el.dataset.providerFilter);
    }
    if (selector === 'select[data-model-filter]') {
      return els.filter(el => el.dataset && el.dataset.modelFilter);
    }
    return [];
  },
  addEventListener: () => {}
};

await import('./portal/static/js/admin.js');
await import('./portal/static/js/room-detail.js');

if (model.value !== 'tiny') throw new Error('Initial model should be tiny');
provider.triggerChange('openai');
if (model.value !== 'whisper-1') throw new Error('Model should switch to whisper-1 for openai');

console.log('ROOM_DETAIL_INTEGRATION_OK');
"""

    result = subprocess.run(
        [node_bin, "--input-type=module", "-e", js_code],
        cwd=os.path.dirname(os.path.dirname(__file__)),
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, f"Node room-detail.js test failed:\nstdout: {result.stdout}\nstderr: {result.stderr}"
    assert "ROOM_DETAIL_INTEGRATION_OK" in result.stdout
