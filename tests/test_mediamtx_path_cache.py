from __future__ import annotations

import logging

import httpx
import pytest

import portal.utils as utils_mod

pytestmark = pytest.mark.anyio

API_BASE = "http://mediamtx:9997"
CHANNEL = "demo/14/en"


class _FakeResponse:
    def __init__(self, status_code: int, payload: dict | None = None, text: str = "") -> None:
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text

    def json(self) -> dict:
        return self._payload


class _FakeClient:
    """Records Control API traffic and answers from a configured-path set."""

    def __init__(self, configured: set[str] | None = None, list_fails: bool = False) -> None:
        self.configured = configured if configured is not None else set()
        self.list_fails = list_fails
        self.added: list[str] = []
        self.patched: list[str] = []
        self.list_calls = 0

    async def get(self, url, params=None, timeout=None):
        self.list_calls += 1
        if self.list_fails:
            raise httpx.ConnectError("control api down")
        items = [{"name": name} for name in sorted(self.configured)]
        return _FakeResponse(200, {"items": items, "pageCount": 1})

    async def post(self, url, json=None, timeout=None):
        name = url.split("/v3/config/paths/add/", 1)[1]
        if name in self.configured:
            return _FakeResponse(400, text="path already exists")
        self.added.append(name)
        self.configured.add(name)
        return _FakeResponse(200)

    async def patch(self, url, json=None, timeout=None):
        name = url.split("/v3/config/paths/patch/", 1)[1]
        self.patched.append(name)
        self.configured.add(name)
        return _FakeResponse(200)


@pytest.fixture
def mtx(monkeypatch):
    """Isolate the module-level path cache and point it at a fake Control API."""
    monkeypatch.setattr(utils_mod.settings, "mediamtx_api_base", API_BASE)
    monkeypatch.setattr(utils_mod, "_created_paths", set())
    monkeypatch.setattr(utils_mod, "_paths_verified_at", 0.0)
    monkeypatch.setattr(utils_mod, "_path_list_outage", utils_mod.OutageLog(utils_mod.logger, "test outage"))

    def _install(client: _FakeClient) -> _FakeClient:
        monkeypatch.setattr(utils_mod, "get_http_client", lambda: client)
        return client

    return _install


async def test_a_new_path_is_added_and_cached(mtx):
    client = mtx(_FakeClient())
    await utils_mod._ensure_mediamtx_path(CHANNEL)
    assert client.added == [CHANNEL]
    assert CHANNEL in utils_mod._created_paths


async def test_a_cached_path_skips_the_add_call(mtx, monkeypatch):
    client = mtx(_FakeClient(configured={CHANNEL}))
    utils_mod._created_paths.add(CHANNEL)
    # Inside the revalidation window the cache is trusted without any call.
    monkeypatch.setattr(utils_mod.time, "monotonic", lambda: 1.0)
    monkeypatch.setattr(utils_mod, "_paths_verified_at", 1.0)
    await utils_mod._ensure_mediamtx_path(CHANNEL)
    assert client.added == []
    assert client.list_calls == 0


async def test_a_mediamtx_restart_invalidates_the_cache(mtx, monkeypatch):
    # The portal believes the path exists; MediaMTX restarted and lost it.
    mtx(_FakeClient(configured=set()))
    utils_mod._created_paths.add(CHANNEL)
    monkeypatch.setattr(utils_mod.time, "monotonic", lambda: 10_000.0)

    await utils_mod._revalidate_created_paths()

    assert CHANNEL not in utils_mod._created_paths


async def test_a_request_after_a_restart_recreates_the_path(mtx, monkeypatch):
    client = mtx(_FakeClient(configured=set()))
    utils_mod._created_paths.add(CHANNEL)
    monkeypatch.setattr(utils_mod.time, "monotonic", lambda: 10_000.0)

    await utils_mod._ensure_mediamtx_path(CHANNEL)

    # Without the fix this asserted nothing: the cache hit returned early.
    assert client.added == [CHANNEL]
    assert CHANNEL in utils_mod._created_paths


async def test_an_existing_path_is_patched_to_restore_always_available(mtx, monkeypatch):
    # MediaMTX still has the name but the portal's cache was cleared.
    client = mtx(_FakeClient(configured={CHANNEL}))
    monkeypatch.setattr(utils_mod.time, "monotonic", lambda: 10_000.0)

    await utils_mod._ensure_mediamtx_path(CHANNEL)

    assert client.patched == [CHANNEL]
    assert CHANNEL in utils_mod._created_paths


async def test_revalidation_keeps_paths_mediamtx_still_has(mtx, monkeypatch):
    mtx(_FakeClient(configured={CHANNEL, "demo/14/fr"}))
    utils_mod._created_paths.update({CHANNEL, "demo/14/fr"})
    monkeypatch.setattr(utils_mod.time, "monotonic", lambda: 10_000.0)

    await utils_mod._revalidate_created_paths()

    assert utils_mod._created_paths == {CHANNEL, "demo/14/fr"}


async def test_revalidation_drops_only_the_missing_paths(mtx, monkeypatch):
    mtx(_FakeClient(configured={CHANNEL}))
    utils_mod._created_paths.update({CHANNEL, "demo/14/fr"})
    monkeypatch.setattr(utils_mod.time, "monotonic", lambda: 10_000.0)

    await utils_mod._revalidate_created_paths()

    assert utils_mod._created_paths == {CHANNEL}


async def test_revalidation_runs_once_per_window(mtx, monkeypatch):
    client = mtx(_FakeClient(configured={CHANNEL}))
    utils_mod._created_paths.add(CHANNEL)

    clock = {"now": 10_000.0}
    monkeypatch.setattr(utils_mod.time, "monotonic", lambda: clock["now"])

    await utils_mod._revalidate_created_paths()
    await utils_mod._revalidate_created_paths()
    assert client.list_calls == 1

    clock["now"] += utils_mod._PATH_REVALIDATE_TTL + 1
    await utils_mod._revalidate_created_paths()
    assert client.list_calls == 2


async def test_revalidation_skips_entirely_when_nothing_is_cached(mtx, monkeypatch):
    client = mtx(_FakeClient())
    monkeypatch.setattr(utils_mod.time, "monotonic", lambda: 10_000.0)

    await utils_mod._revalidate_created_paths()

    assert client.list_calls == 0


async def test_an_unreachable_control_api_is_non_fatal_and_keeps_the_cache(mtx, monkeypatch):
    mtx(_FakeClient(configured={CHANNEL}, list_fails=True))
    utils_mod._created_paths.add(CHANNEL)
    monkeypatch.setattr(utils_mod.time, "monotonic", lambda: 10_000.0)

    await utils_mod._revalidate_created_paths()

    # Fail open: the path cannot be recreated while the API is down either.
    assert CHANNEL in utils_mod._created_paths


async def test_an_unreachable_control_api_is_observable(mtx, monkeypatch, caplog):

    mtx(_FakeClient(list_fails=True))
    utils_mod._created_paths.add(CHANNEL)
    monkeypatch.setattr(utils_mod.time, "monotonic", lambda: 10_000.0)

    with caplog.at_level(logging.WARNING, logger=utils_mod.logger.name):
        await utils_mod._revalidate_created_paths()

    assert any("test outage" in r.getMessage() for r in caplog.records)


async def test_a_failing_control_api_does_not_stampede(mtx, monkeypatch):
    client = mtx(_FakeClient(list_fails=True))
    utils_mod._created_paths.add(CHANNEL)
    monkeypatch.setattr(utils_mod.time, "monotonic", lambda: 10_000.0)

    await utils_mod._revalidate_created_paths()
    await utils_mod._revalidate_created_paths()

    # The window advances on failure too, so an outage is not hammered.
    assert client.list_calls == 1


async def test_a_path_added_during_the_list_call_is_not_dropped(mtx, monkeypatch):
    """A concurrent ensure must not have its cache entry evicted (race)."""

    class _SlowList(_FakeClient):
        async def get(self, url, params=None, timeout=None):
            # Stand in for the await: another request adds a path mid-flight.
            utils_mod._created_paths.add("demo/14/de")
            return await super().get(url, params=params, timeout=timeout)

    mtx(_SlowList(configured={CHANNEL}))
    utils_mod._created_paths.add(CHANNEL)
    monkeypatch.setattr(utils_mod.time, "monotonic", lambda: 10_000.0)

    await utils_mod._revalidate_created_paths()

    assert "demo/14/de" in utils_mod._created_paths
    assert CHANNEL in utils_mod._created_paths


async def test_a_failed_check_retries_sooner_than_a_full_window(mtx, monkeypatch):
    client = mtx(_FakeClient(list_fails=True))
    utils_mod._created_paths.add(CHANNEL)
    clock = {"now": 10_000.0}
    monkeypatch.setattr(utils_mod.time, "monotonic", lambda: clock["now"])

    await utils_mod._revalidate_created_paths()
    assert client.list_calls == 1

    clock["now"] += utils_mod._PATH_REVALIDATE_RETRY_TTL + 0.1
    await utils_mod._revalidate_created_paths()
    assert client.list_calls == 2


@pytest.mark.parametrize(
    "payload",
    [
        ["not", "a", "dict"],
        {"items": "not-a-list", "pageCount": 1},
        {"items": [{"name": CHANNEL}], "pageCount": "lots"},
        {"items": ["not-a-dict"], "pageCount": 1},
        {"items": [{"name": 42}], "pageCount": 1},
        # Falsey wrong types: these must not be read as "MediaMTX has no paths".
        {"items": {}, "pageCount": 1},
        {"items": 0, "pageCount": 1},
        {"items": "", "pageCount": 1},
        {"items": False, "pageCount": 1},
        # MediaMTX's schema requires items as a list; absent or null is broken,
        # not an empty MediaMTX. Treating it as empty would silently evict
        # every cached path on a response that is in fact malformed.
        {"pageCount": 1},
        {"items": None, "pageCount": 1},
    ],
)
async def test_a_malformed_list_response_keeps_the_cache_and_does_not_raise(mtx, monkeypatch, payload):
    class _Malformed(_FakeClient):
        async def get(self, url, params=None, timeout=None):
            self.list_calls += 1
            return _FakeResponse(200, payload)

    mtx(_Malformed())
    utils_mod._created_paths.add(CHANNEL)
    monkeypatch.setattr(utils_mod.time, "monotonic", lambda: 10_000.0)

    # A bad payload must not surface as a 500 on the triggering request.
    await utils_mod._revalidate_created_paths()

    assert CHANNEL in utils_mod._created_paths


async def test_an_empty_items_list_still_evicts_cached_paths(mtx, monkeypatch):
    """An explicit empty list is the one shape that legitimately means "none"."""

    class _Empty(_FakeClient):
        async def get(self, url, params=None, timeout=None):
            self.list_calls += 1
            return _FakeResponse(200, {"items": [], "pageCount": 1})

    mtx(_Empty())
    utils_mod._created_paths.add(CHANNEL)
    monkeypatch.setattr(utils_mod.time, "monotonic", lambda: 10_000.0)

    await utils_mod._revalidate_created_paths()

    # MediaMTX authoritatively has nothing, so the stale entry is dropped.
    assert CHANNEL not in utils_mod._created_paths


async def test_a_truncated_page_run_does_not_evict_anything(mtx, monkeypatch):
    """Hitting the page cap means the list is incomplete, not authoritative."""

    class _EndlessPages(_FakeClient):
        async def get(self, url, params=None, timeout=None):
            self.list_calls += 1
            return _FakeResponse(200, {"items": [], "pageCount": 10_000})

    client = mtx(_EndlessPages())
    utils_mod._created_paths.add(CHANNEL)
    monkeypatch.setattr(utils_mod.time, "monotonic", lambda: 10_000.0)

    await utils_mod._revalidate_created_paths()

    assert CHANNEL in utils_mod._created_paths
    assert client.list_calls == utils_mod._MAX_PATH_LIST_PAGES


async def test_every_page_of_a_multi_page_list_is_read(mtx, monkeypatch):
    class _TwoPages(_FakeClient):
        async def get(self, url, params=None, timeout=None):
            self.list_calls += 1
            page = (params or {}).get("page", 0)
            items = [{"name": CHANNEL}] if page == 0 else [{"name": "demo/14/fr"}]
            return _FakeResponse(200, {"items": items, "pageCount": 2})

    mtx(_TwoPages())
    utils_mod._created_paths.update({CHANNEL, "demo/14/fr"})
    monkeypatch.setattr(utils_mod.time, "monotonic", lambda: 10_000.0)

    await utils_mod._revalidate_created_paths()

    # A name only on page 2 must not be mistaken for a dropped path.
    assert utils_mod._created_paths == {CHANNEL, "demo/14/fr"}


async def test_revalidation_is_skipped_without_a_control_api_base(mtx, monkeypatch):
    client = mtx(_FakeClient())
    monkeypatch.setattr(utils_mod.settings, "mediamtx_api_base", "")
    utils_mod._created_paths.add(CHANNEL)
    monkeypatch.setattr(utils_mod.time, "monotonic", lambda: 10_000.0)

    await utils_mod._revalidate_created_paths()

    assert client.list_calls == 0
    assert CHANNEL in utils_mod._created_paths


async def test_the_floor_path_keeps_last_publisher_wins_disabled(mtx):
    """The floor pseudo-booth must never take a publisher by race (invariant 3)."""
    sent: dict = {}

    class _Recording(_FakeClient):
        async def post(self, url, json=None, timeout=None):
            sent.update(json or {})
            return await super().post(url, json=json, timeout=timeout)

    mtx(_Recording())
    await utils_mod._ensure_mediamtx_path("demo/14/floor")

    assert sent == {"alwaysAvailable": False, "overridePublisher": False}
