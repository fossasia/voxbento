# Background job scheduling

Webhook delivery, transactional email and meeting analysis can use Redis for
scheduling. Database polling is the default (`JOB_QUEUE_BACKEND=db`), and Redis
is never required for the portal to start. Keep one portal replica: this feature
does not distribute booth state or real-time media workers.

## Enable Redis

Apply migrations with `uv run alembic upgrade head`. Migration 048 adds queue
discovery indexes and the execution start timestamp for meeting reports.

For Docker Compose, set these in `.env`:

```dotenv
COMPOSE_PROFILES=queues
JOB_QUEUE_BACKEND=redis
REDIS_URL=redis://redis:6379/0
```

Then run `docker compose up -d --build`. The Redis service has a persistent
volume and no host port. The portal has no startup dependency on Redis. A native
portal can use an operator-managed instance at `redis://localhost:6379/0`.
Use `rediss://` and credentials when connecting to a secured remote instance;
do not expose the Compose Redis service publicly. Use a distinct
`JOB_QUEUE_PREFIX` (default `voxbento:jobs`) for each deployment sharing Redis.

`JOB_QUEUE_RECONCILE_SECONDS` defaults to 60 (range 1–3600). On startup and at
this interval, each worker scans unfinished rows in batches of 100 and restores
jobs due within the next reconciliation interval. Scheduled jobs further in the
future stay in the database until they enter that window.

## Delivery and recovery

Redis sorted sets contain only job IDs, scored by their due UTC timestamp. Email
bodies, recipients, webhook payloads and report transcripts stay in the database.
Creating an email or webhook stages its ID in the current DB transaction;
`get_session()` publishes it only after the outer commit succeeds. Savepoint
rollback discards that savepoint's hints. Analysis is scheduled after its report
creation transaction commits. Cancellation or a Redis error between commit and
enqueue leaves the row available for reconciliation.

Workers atomically pop due hints, then claim the matching row in the database.
Duplicate, deleted, finished or premature hints cannot bypass the DB eligibility
checks. Email expiration, token suppression, encryption and claim recovery remain
in force. Webhook signatures, vetted-IP dispatch, circuit breaker, four attempts
and retry delays are unchanged. Email still uses four attempts with exponential
delays starting at 15 seconds, capped at 300 seconds. Retries are scheduled only
after their status and due time commit.

When Redis is unavailable, workers use the existing database polling paths;
connection attempts have bounded timeouts. Redis recovery triggers another DB
reconciliation. Even if Redis loses all queue data, committed unfinished work is
rediscovered at the next reconciliation. Email leases are reclaimed after expiry.
Webhooks retain their existing transaction around dispatch, so a worker crash
rolls back its claim. Redis persistence is useful but is not the durability boundary.

Meeting reports are atomically claimed from pending to running. Pending reports
are rediscovered even without Redis; they are not failed just for waiting in the
queue. Execution has the existing 25-minute timeout. Running reports older than
30 minutes from `started_at` (or `created_at` for pre-migration rows) are marked
failed; they are not automatically rerun, which could repeat paid AI calls. Users
can generate a new report as before.

Delivery remains at least once: a process can stop after SMTP or a webhook target
accepts a request but before the DB records success. Consumers should deduplicate
webhooks using the stable `delivery_id`; SMTP cannot guarantee exactly-once sending.

To disable Redis scheduling, set `JOB_QUEUE_BACKEND=db` and recreate the portal.
Pending jobs continue from their DB records. The Redis service can then be stopped.

Audio, transcription, TTS, live captions, booth state, the live demo and YouTube
caption posting keep their existing paths and are not scheduled here.
