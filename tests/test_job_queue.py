from __future__ import annotations

import asyncio
import socket
from datetime import timedelta
from unittest.mock import AsyncMock

import fakeredis.aioredis
import httpx
import pytest
from redis.exceptions import ConnectionError as RedisConnectionError
from sqlalchemy import select

from portal.config import Settings, settings
from portal.database import configure, create_event, create_room, create_user, dispose, get_session, init_db
from portal.job_queue import Job, RedisJobQueue, defer_job, job_queue, timestamp
from portal.meeting_analysis import worker as analysis
from portal.models import (
    DeveloperAccount,
    EmailDelivery,
    MeetingReport,
    TranscriptSegment,
    WebhookDelivery,
    WebhookSubscription,
    utc_now,
)
from portal.notifications import _claim_delivery, _record_failure, _send_delivery, queue_notification
from portal.webhooks import worker as webhooks
from portal.webhooks.worker import enqueue_webhook, process_due_webhooks


@pytest.fixture
async def queues(tmp_path, monkeypatch):
    configure(f"sqlite+aiosqlite:///{tmp_path / 'jobs.db'}")
    await init_db()
    await job_queue.close()
    monkeypatch.setattr(settings, "job_queue_backend", "redis")
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(job_queue, "_redis", redis)
    yield redis
    await job_queue.close()
    await dispose()


async def email(session, **kwargs):
    return await queue_notification(
        session, "verification", "person@example.com", {"verification_link": "https://example.invalid/secret"}, **kwargs
    )


@pytest.mark.anyio
async def test_enqueue_follows_commit_and_does_not_publish_payload(queues, monkeypatch):
    original_schedule = job_queue.schedule

    async def after_commit(jobs):
        async with get_session() as committed_session:
            assert await committed_session.get(EmailDelivery, jobs[0].id) is not None
        return await original_schedule(jobs)

    monkeypatch.setattr(job_queue, "schedule", after_commit)
    async with get_session() as session:
        delivery = await email(session)
        assert await queues.keys("*") == []
    assert await queues.zrange(job_queue.key("email"), 0, -1) == [str(delivery.id)]
    assert await queues.type(job_queue.key("email")) == "zset"
    async with get_session() as session:
        assert await session.get(EmailDelivery, delivery.id) is not None


@pytest.mark.anyio
async def test_failed_outer_transaction_never_enqueues(queues):
    with pytest.raises(ValueError):
        async with get_session() as session:
            await email(session)
            raise ValueError("rollback")
    assert await queues.keys("*") == []
    async with get_session() as session:
        assert await session.scalar(select(EmailDelivery.id)) is None


@pytest.mark.anyio
async def test_commit_failure_never_publishes_staged_jobs(queues):
    from sqlalchemy import event

    def refuse_commit(session):
        raise ValueError("commit failed")

    with pytest.raises(ValueError, match="commit failed"):
        async with get_session() as session:
            await email(session)
            event.listen(session.sync_session, "before_commit", refuse_commit)
    assert await queues.keys("*") == []


@pytest.mark.anyio
async def test_savepoint_jobs_wait_for_outer_commit_and_rollback_discards_only_nested_jobs(queues):
    async with get_session() as session:
        outer = await email(session)
        async with session.begin_nested():
            nested = await email(session)
        assert await queues.keys("*") == []
        with pytest.raises(ValueError):
            async with session.begin_nested():
                await email(session)
                raise ValueError("savepoint rollback")
    assert set(await queues.zrange(job_queue.key("email"), 0, -1)) == {str(outer.id), str(nested.id)}


@pytest.mark.anyio
async def test_outer_rollback_discards_committed_savepoint_jobs(queues):
    with pytest.raises(ValueError):
        async with get_session() as session:
            # Force a real outer write before SQLite opens the savepoint.
            await email(session)
            async with session.begin_nested():
                await email(session)
            raise ValueError("outer rollback")
    assert await queues.keys("*") == []


@pytest.mark.anyio
async def test_explicit_rollback_never_enqueues(queues):
    async with get_session() as session:
        await email(session)
        await session.rollback()
    assert await queues.keys("*") == []


@pytest.mark.anyio
async def test_db_default_never_connects_to_redis(queues, monkeypatch):
    monkeypatch.setattr(settings, "job_queue_backend", "db")
    monkeypatch.setattr(job_queue, "client", lambda: pytest.fail("DB mode must not connect"))
    async with get_session() as session:
        await email(session)
    assert await job_queue.ready("email") is None


@pytest.mark.anyio
async def test_redis_outage_after_commit_keeps_email_and_db_fallback_sends_it(queues, monkeypatch):
    monkeypatch.setattr(settings, "smtp_host", "smtp.example.invalid")
    send = AsyncMock()
    monkeypatch.setattr("portal.notifications.FastMail.send_message", send)
    monkeypatch.setattr(job_queue, "client", lambda: (_ for _ in ()).throw(RedisConnectionError("private-url")))
    async with get_session() as session:
        delivery = await email(session)
    assert await job_queue.ready("email") is None
    from portal.notifications import process_due_deliveries

    assert await process_due_deliveries() == 1
    send.assert_awaited_once()
    async with get_session() as session:
        assert (await session.get(EmailDelivery, delivery.id)).status == "accepted"


@pytest.mark.anyio
async def test_due_pop_is_atomic_and_future_jobs_stay_scheduled(queues):
    now = utc_now()
    await job_queue.schedule([Job("email", 1, now), Job("email", 2, now + timedelta(minutes=5))])
    job_queue._reconciled["email"] = float("inf")
    first, second = await asyncio.gather(job_queue.ready("email"), job_queue.ready("email"))
    assert sorted(first + second) == [1]
    assert await queues.zrange(job_queue.key("email"), 0, -1) == ["2"]


@pytest.mark.anyio
async def test_email_claim_rejects_duplicate_premature_and_expired_hints(queues):
    async with get_session() as session:
        due = await email(session)
        future = await email(session, scheduled_at=utc_now() + timedelta(hours=1))
        expired = await email(session, expires_at=utc_now() - timedelta(seconds=1))
    assert await _claim_delivery(due.id) == due.id
    assert await _claim_delivery(due.id) is None
    assert await _claim_delivery(future.id) is None
    assert await _claim_delivery(expired.id) is None


@pytest.mark.anyio
async def test_concurrent_email_claims_increment_attempts_once(queues):
    async with get_session() as session:
        delivery = await email(session)
    claims = await asyncio.gather(_claim_delivery(delivery.id), _claim_delivery(delivery.id))
    assert claims.count(delivery.id) == 1
    assert claims.count(None) == 1
    async with get_session() as session:
        assert (await session.get(EmailDelivery, delivery.id)).attempt_count == 1


@pytest.mark.anyio
async def test_connection_recovery_immediately_reconciles_committed_work(queues, monkeypatch):
    with monkeypatch.context() as offline:
        offline.setattr(job_queue, "client", lambda: (_ for _ in ()).throw(RedisConnectionError("offline")))
        async with get_session() as session:
            delivery = await email(session)
        assert await job_queue.ready("email") is None
    assert await job_queue.ready("email") == [delivery.id]


@pytest.mark.anyio
async def test_real_client_connection_failure_falls_back_without_internal_retries(queues, monkeypatch):
    # Reserve a local port without listening, so no other service can accept this connection.
    with socket.socket() as reserved:
        reserved.bind(("127.0.0.1", 0))
        monkeypatch.setattr(settings, "redis_url", f"redis://127.0.0.1:{reserved.getsockname()[1]}/0")
        client_queue = RedisJobQueue()
        try:
            assert await asyncio.wait_for(client_queue.ready("email"), timeout=2) is None
        finally:
            await client_queue.close()


@pytest.mark.anyio
async def test_email_retry_uses_persisted_due_time_and_attempt_limit(queues):
    async with get_session() as session:
        delivery = await email(session)
    assert await _claim_delivery(delivery.id) == delivery.id
    await queues.flushall()
    before = utc_now()
    await _record_failure(delivery.id, "SMTP unavailable")
    score = await queues.zscore(job_queue.key("email"), str(delivery.id))
    async with get_session() as session:
        stored = await session.get(EmailDelivery, delivery.id)
        assert stored.status == "pending"
        assert score == timestamp(stored.scheduled_at)
        assert score >= timestamp(before + timedelta(seconds=15))
        stored.status = "sending"
        stored.attempt_count = 4
    await queues.flushall()
    await _record_failure(delivery.id, "last failure")
    assert await queues.keys("*") == []
    async with get_session() as session:
        assert (await session.get(EmailDelivery, delivery.id)).status == "failed"


@pytest.mark.anyio
async def test_reconcile_restores_lost_hints_and_expired_email_leases(queues):
    async with get_session() as session:
        due = await email(session)
        waiting = await email(session, scheduled_at=utc_now() + timedelta(days=1))
        sending = await email(session)
        sending.status = "sending"
        sending.claim_until = utc_now() - timedelta(seconds=1)
        expired = await email(session, expires_at=utc_now() - timedelta(seconds=1))
    await queues.flushall()
    assert set(await job_queue.ready("email")) == {due.id, sending.id}
    async with get_session() as session:
        assert (await session.get(EmailDelivery, expired.id)).status == "expired"
    assert await _claim_delivery(waiting.id) is None
    # Redis can lose data while connections remain healthy; the periodic scan still repairs it.
    job_queue._reconciled.clear()
    assert set(await job_queue.ready("email")) == {due.id, sending.id}


@pytest.mark.anyio
async def test_reconcile_covers_more_than_one_batch_without_starvation(queues):
    async with get_session() as session:
        for _ in range(105):
            session.add(
                EmailDelivery(
                    notification_key="verification",
                    recipient="a@example.com",
                    encrypted_subject="s",
                    encrypted_html="h",
                )
            )
    assert await job_queue.reconcile("email")
    assert await queues.zcard(job_queue.key("email")) == 105


@pytest.mark.anyio
async def test_healthy_idle_redis_does_not_scan_database_every_tick(queues, monkeypatch):
    assert await job_queue.ready("email") == []
    monkeypatch.setattr(job_queue, "reconcile", AsyncMock(side_effect=AssertionError("unnecessary DB scan")))
    for _ in range(3):
        assert await job_queue.ready("email") == []


async def subscription():
    async with get_session() as session:
        user = await create_user(session, email="dev@example.com", display_name="Developer")
        account = DeveloperAccount(user_id=user.id, status="approved")
        session.add(account)
        await session.flush()
        sub = WebhookSubscription(
            developer_account_id=account.id,
            target_url="https://example.com/hook",
            event_types=["test"],
            secret_key="secret",
        )
        session.add(sub)
        await session.flush()
        return sub.id


@pytest.mark.anyio
async def test_webhook_enqueue_retry_reconcile_and_duplicate_delivery(queues, monkeypatch):
    await subscription()
    # conftest disables the module attribute, so call the function captured at import.
    await enqueue_webhook("test", {"private": "payload"})
    ids = await job_queue.ready("webhook")
    assert len(ids) == 1
    delivery_id = ids[0]
    monkeypatch.setattr(webhooks, "validate_ssrf", AsyncMock(return_value=["93.184.216.34"]))
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(503))) as client:
        before = utc_now()
        assert await process_due_webhooks(client, delivery_id) == 1
        async with get_session() as session:
            row = await session.get(WebhookDelivery, delivery_id)
            assert (row.status, row.attempt_count) == ("failed", 1)
            assert timestamp(row.next_attempt_at) >= timestamp(before + timedelta(seconds=2))
            assert await queues.zscore(job_queue.key("webhook"), str(delivery_id)) == timestamp(row.next_attempt_at)
        assert await process_due_webhooks(client, delivery_id) == 0
    await queues.flushall()
    assert await job_queue.reconcile("webhook")
    assert await queues.zcard(job_queue.key("webhook")) == 1
    async with get_session() as session:
        row = await session.get(WebhookDelivery, delivery_id)
        row.next_attempt_at = utc_now() - timedelta(seconds=1)
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200))) as client:
        assert await process_due_webhooks(client, delivery_id) == 1
        assert await process_due_webhooks(client, delivery_id) == 0


@pytest.mark.anyio
async def test_webhook_processing_exception_rolls_back_claim_for_reconcile(queues, monkeypatch):
    await subscription()
    await enqueue_webhook("test", {})
    (delivery_id,) = await job_queue.ready("webhook")
    monkeypatch.setattr(webhooks, "process_delivery", AsyncMock(side_effect=RuntimeError("interrupted")))
    async with httpx.AsyncClient() as client:
        with pytest.raises(RuntimeError, match="interrupted"):
            await process_due_webhooks(client, delivery_id)
    async with get_session() as session:
        assert (await session.get(WebhookDelivery, delivery_id)).status == "pending"
    await job_queue.reconcile("webhook")
    assert await job_queue.ready("webhook") == [delivery_id]


@pytest.mark.anyio
async def test_invalid_queue_members_are_discarded_without_poisoning_worker(queues):
    job_queue._reconciled["email"] = float("inf")
    await queues.zadd(job_queue.key("email"), {"payload": 0, "-1": 0, "9" * 30: 0, "1": 0})
    assert await job_queue.ready("email") == [1]


def test_invalid_backend_is_rejected_and_db_is_default():
    from pydantic import ValidationError

    assert Settings(_env_file=None).job_queue_backend == "db"
    with pytest.raises(ValidationError):
        Settings(_env_file=None, job_queue_backend="unknown")


async def report(**fields):
    async with get_session() as session:
        event = await create_event(session, slug="summit", display_name="Summit")
        room = await create_room(session, event_id=event.id, display_name="Main")
        session.add(TranscriptSegment(room_id=room.id, language_code="floor", text="We agreed to ship captions."))
        row = MeetingReport(
            event_id=event.id,
            room_id=room.id,
            channel_id="floor",
            channel_label="Floor",
            language_code="en",
            provider="local",
            model="gemma3:4b",
            **fields,
        )
        session.add(row)
        await session.flush()
        return row.id


@pytest.mark.anyio
async def test_analysis_claim_is_atomic_and_duplicate_execution_is_skipped(queues, monkeypatch):
    from portal.meeting_analysis.providers import Completion

    provider = AsyncMock()
    provider.complete.return_value = Completion(
        '{"title":"Decisions","overview":"Ship captions.","sections":[{"heading":"Plan","points":["Ship captions"]}]}',
        12,
    )
    monkeypatch.setattr(analysis, "get_provider", lambda _: provider)
    report_id = await report()
    await asyncio.gather(analysis.run_analysis(report_id), analysis.run_analysis(report_id))
    provider.complete.assert_awaited_once()
    async with get_session() as session:
        row = await session.get(MeetingReport, report_id)
        assert row.status == "done"
        assert row.started_at is not None


@pytest.mark.anyio
async def test_redis_loss_restores_old_pending_analysis_without_retiring_it(queues):
    report_id = await report(created_at=utc_now() - timedelta(hours=2))
    await analysis.schedule_analysis(report_id)
    await queues.flushall()
    async with get_session() as session:
        await analysis.mark_interrupted_reports(session)
    assert await job_queue.ready("analysis") == [report_id]
    assert await analysis._prepare(report_id) is not None
    async with get_session() as session:
        await analysis.mark_interrupted_reports(session)
    async with get_session() as session:
        assert (await session.get(MeetingReport, report_id)).status == "running"


@pytest.mark.anyio
async def test_analysis_loop_recovers_pending_reports_when_redis_is_down(queues, monkeypatch):
    report_id = await report()
    launched = []
    monkeypatch.setattr(job_queue, "ready", AsyncMock(return_value=None))
    monkeypatch.setattr(analysis, "launch_analysis", launched.append)

    async def stop(_):
        raise asyncio.CancelledError

    monkeypatch.setattr(analysis.asyncio, "sleep", stop)
    with pytest.raises(asyncio.CancelledError):
        await analysis.analysis_worker_loop()
    assert launched == [report_id]


@pytest.mark.anyio
async def test_analysis_enqueue_failure_launches_existing_runner(queues, monkeypatch):
    report_id = await report()
    monkeypatch.setattr(job_queue, "schedule", AsyncMock(return_value=False))
    launched = []
    monkeypatch.setattr(analysis, "launch_analysis", launched.append)
    await analysis.schedule_analysis(report_id)
    assert launched == [report_id]
