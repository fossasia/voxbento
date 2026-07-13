"""Tests for demo asset generation concurrency and status reporting.

Covers:
- /api/demo/manifest reports "pending" before first generation
- Two near-simultaneous /admin/demo/regenerate calls are serialized: only one
  starts generation, the other is told generation is already in progress
- The background task is tracked and status transitions generating -> ready
"""

from __future__ import annotations

import asyncio
import json
import os

os.environ["BOOTH_ACCESS_TOKEN"] = ""
os.environ["ADMIN_PASSWORD"] = "test-admin-pass"

import pytest

from portal.config import settings

settings.admin_password = "test-admin-pass"


@pytest.fixture(autouse=True)
def demo_state(tmp_path, monkeypatch):
    """Isolate demo generation module state and manifest path between tests."""
    from portal.tts import demo_gen as dg

    monkeypatch.setattr(dg, "_generating", False)
    monkeypatch.setattr(dg, "_generation_error", None)
    monkeypatch.setattr(dg, "MANIFEST_PATH", tmp_path / "manifest.json")
    dg._tasks.clear()
    yield dg
    for task in list(dg._tasks):
        task.cancel()
    dg._tasks.clear()


@pytest.mark.anyio
async def test_manifest_pending_before_first_generation(demo_state):
    from portal.routers.demo import demo_manifest

    resp = await demo_manifest()
    assert json.loads(resp.body)["status"] == "pending"


@pytest.mark.anyio
async def test_regenerate_marks_generating_then_ready(demo_state, monkeypatch):
    from portal.routers.demo import demo_manifest, regenerate_demo

    dg = demo_state

    async def fake_generate():
        dg.MANIFEST_PATH.write_text(json.dumps({"video_url": "x", "languages": []}))
        return {"video_url": "x", "languages": []}

    monkeypatch.setattr(dg, "generate_demo_assets", fake_generate)

    resp = await regenerate_demo()
    assert json.loads(resp.body) == {"ok": True, "detail": "Generation started"}

    # The task is scheduled but hasn't run yet: regenerate_demo() sets the
    # flag synchronously, so the status already reflects "generating".
    status_resp = await demo_manifest()
    assert json.loads(status_resp.body)["status"] == "generating"

    (task,) = dg._tasks
    await task

    status_resp = await demo_manifest()
    assert json.loads(status_resp.body)["status"] == "ready"


@pytest.mark.anyio
async def test_concurrent_regenerate_is_serialized(demo_state, monkeypatch):
    """Two near-simultaneous regenerate calls must not both start generation.

    Regression test: previously `_generating` was only set inside the
    scheduled task body (not before `asyncio.create_task()` was called), so a
    second request arriving before the event loop ran the first task's body
    would also pass the "already generating" check and start a second,
    overlapping generation.
    """
    from portal.routers.demo import regenerate_demo

    dg = demo_state

    async def fake_generate():
        await asyncio.sleep(0.01)
        return {"video_url": "x", "languages": []}

    monkeypatch.setattr(dg, "generate_demo_assets", fake_generate)

    first, second = await asyncio.gather(regenerate_demo(), regenerate_demo())
    oks = sorted(json.loads(r.body)["ok"] for r in (first, second))
    assert oks == [False, True]

    for task in list(dg._tasks):
        await task
