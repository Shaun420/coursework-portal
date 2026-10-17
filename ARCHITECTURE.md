# Architecture

The portal is one Python process. Every layer is behind an interface so that
the parts that do not scale on a single machine can be replaced without
touching the rest.

```
                        ┌──────────────────────────────┐
   browser ──HTTP──►    │  raw-ASGI middleware stack    │   metrics, CSRF,
                        │  GZip · CSRF · Metrics        │   gzip
                        └──────────────┬───────────────┘
                                       │
              ┌────────────────────────┴────────────────────────┐
              │                  FastAPI app                    │
              │   app/web (Jinja2 pages)   app/api (JSON)       │
              └───────┬───────────────────────────────┬─────────┘
                      │                               │
        ┌─────────────┴──────────────┐   ┌────────────┴─────────────────┐
        │  services                  │   │  services                    │
        │  auth · submissions ·      │   │  drive/  (DriveProvider)     │
        │  analysis                  │   │  similarity/ · detection/    │
        └───────┬────────────────────┘   └────────────┬─────────────────┘
                │                                     │
        ┌───────┴────────┐                   ┌────────┴────────┐
        │ db/ repositories│                  │ workers/ queue  │
        │ SQLAlchemy async│                  │ + CPU pool      │
        └───────┬────────┘                   └─────────────────┘
                │
        ┌───────┴────────┐
        │ SQLite (WAL)    │  ──►  Postgres, config swap
        └────────────────┘
```

---

## Layering

**`app/domain`** — the only place that knows the shape of the data. SQLAlchemy
entities, enums, Pydantic schemas. No I/O.

**`app/db/repositories.py`** — every query lives here. Endpoints and services
never build a `select()`. This is what makes the database swappable and what
let the N+1 queries be fixed in one place rather than five.

**`app/services`** — business rules. `submissions.py` owns the submit
transaction; `analysis.py` owns scoring; `auth.py` owns sessions. Services
take repositories, not sessions.

**`app/services/drive`** — a `DriveProvider` protocol with two implementations.
`mock` writes to the local filesystem; `google` speaks Drive v3 over a shared
`httpx.AsyncClient`. Nothing above this layer knows which is in use. Swapping
in S3 or SharePoint is a third implementation.

**`app/api`** and **`app/web`** — thin. Handlers authenticate, call a service,
format a response.

**`app/services/workers`** — the CPU-bound analysis pipeline, described below.

---

## Concurrency model

### Everything is async, and nothing blocks the loop

Text extraction (`pypdf`, `python-docx`), hashing and the stylometric feature
pass are CPU-bound and would stall the event loop. They run in a
`ThreadPoolExecutor` sized to `ANALYSIS_CPU_THREADS` (default 2, i.e. the
number of cores) via `WorkerPool.run_blocking`. Filesystem I/O in the mock
Drive provider goes through `asyncio.to_thread` for the same reason.

### No connection is ever held across a network call

This was the single most important fix. The first version opened a database
session, then called Drive, then committed — holding a pooled connection for
the entire Drive round trip. At 200 concurrent users that exhausted the pool
(`QueuePool limit of size 20 overflow 40 reached, connection timed out`) and
starved even read-only requests.

`app/services/submissions.py` is now structured as a sequence of **short**
transactions with the Drive calls *between* them:

```
session_scope()  → insert submission row, COMMIT
Drive            → upload to teacher's Drive        ← no DB connection held
session_scope()  → store path + metadata, COMMIT
on_commit        → enqueue analysis job
```

Two consequences worth noting:

- A Drive outage can no longer lose a submission — the row is committed before
  the upload is attempted.
- Background jobs are enqueued from SQLAlchemy's `after_commit` event, not
  inside the transaction. Enqueueing earlier let a worker pick up a job whose
  row was not yet visible, producing intermittent
  `LookupError: submission N does not exist`.

### The auth dependency does not pin a connection

A FastAPI dependency's session lives until the response has been sent. When
`current_user` depended on the request-scoped session, **every** request held
a pooled connection for its whole lifetime — including handlers that then
opened further sessions of their own. At a few hundred concurrent users that
alone exhausted the pool. `current_user` now opens a short session, loads the
user, and releases it immediately (`expire_on_commit=False` keeps the detached
instance usable).

### Sessions are stateless

A session cookie is an HMAC-signed `(user_id, role, issued_at)` tuple
verified against `PORTAL_SECRET_KEY`. There is no server-side session table
and no Redis. Any replica can serve any request, which is what makes
horizontal scaling a `--scale` flag rather than a rearchitecture. CSRF uses
the double-submit pattern with a token derived from the session value and
compared with `hmac.compare_digest`.

### SQLite: one writer, many readers

WAL mode means readers never block the writer, and a real connection pool
(`AsyncAdaptedQueuePool`, not aiosqlite's default `NullPool`) turns every
pooled connection into a concurrent reader.

Two non-obvious rules fall out of this:

1. **`busy_timeout` does not apply to a deferred read transaction upgraded to
   a write.** It fails immediately with `database is locked`. Mutating
   endpoints therefore use `get_db_write()`, which issues `BEGIN IMMEDIATE`
   so writers queue politely instead of erroring. Raising `busy_timeout` does
   not fix this.
2. **Writers go through an `asyncio.Semaphore(1)`** (`write_gate()`) so they
   wait *before* taking a pooled connection. Without it, a write burst consumed
   the entire pool and read-only requests started failing.

Both are SQLite-only; on Postgres `write_gate()` is a `nullcontext` and
`BEGIN IMMEDIATE` is not issued.

### Saturation returns 503, not 500

When the pool is exhausted (`TimeoutError`), the database is busy
(`OperationalError`), or the analysis queue is full (`QueueFullError`), the
client gets `503` with a `Retry-After` header. A 500 would be a lie — the
request was not malformed, the server is simply busy — and it would make
retry storms worse.

### Bounded queue with backpressure

`JobQueue` is bounded (`ANALYSIS_QUEUE_CAPACITY`, default 5000). When it is
full, submission still succeeds — the work is durable — and the analysis is
refused with a 503 rather than silently dropped or allowed to grow the heap
without limit. Failed jobs retry twice, then are dropped with a log line.

### Middleware is raw ASGI

Three layers of Starlette `BaseHTTPMiddleware` wrapped every request in an
anyio task group and were the throughput ceiling on trivial endpoints.
Rewriting `CsrfMiddleware` and `MetricsMiddleware` as plain ASGI callables
roughly doubled `/healthz` throughput at concurrency 100 (190 → 417 req/s).

---

## The integrity pipeline

On submit:

1. Extract text from the uploaded file (`pypdf` / `python-docx` / plain text).
   Extraction failure records `SKIPPED` rather than failing the submission —
   a scan-only PDF should not block a student from handing in work.
2. **Similarity.** Three signals, blended
   `0.45 × verbatim + 0.35 × peer + 0.20 × reference`:
   - *Verbatim blocks* — 40-token word-aligned windows hashed with blake2b,
     step 10. Word alignment matters: character-window matching missed
     reflowed copies entirely.
   - *Peer comparison* — MinHash signatures (128 permutations, 5-word
     shingles, prime `2⁶¹−1`) banded 32-way for LSH, with a full scan as
     fallback. Calibrated against a `JACCARD_CALIBRATION_CEILING` of 0.85.
   - *Reference overlap* — against material the instructor uploaded, with a
     `REFERENCE_ALLOWANCE` of 0.25 so quoting the assignment brief is not
     plagiarism.
3. **AI detection.** 19 weighted stylometric features — sentence-length
   burstiness, lexical diversity (TTR), repeated 4-gram ratio, an AI-marker
   lexicon, punctuation rhythm, hedging density. Each contributes
   `score × weight`; `confidence` combines the log-growth of document length
   with inter-feature agreement. Documents under `AI_DETECTION_MIN_WORDS`
   (120) are marked `reliable=False` rather than scored on noise.
4. **Verdict.** `flagged = similarity ≥ 0.50 or ai ≥ 0.50`.
5. **Publish.** The report JSON and a per-assignment `roster_summary.csv` go
   to the instructor's Drive.

Fingerprints live in their own `document_fingerprint` table rather than
embedded in report JSON, so a peer comparison is a table scan rather than a
deserialization of every past report.

The similarity index is **directional** — "how much of this submission comes
from elsewhere", not a symmetric similarity between two documents. That is
deliberate and it is what makes the score interpretable as an integrity
signal.

The AI detector is a heuristic stylometric ensemble, and every report says so
in plain terms. It is a triage signal for a human reviewer, not proof.

---

## Security

- OAuth 2.0 authorization-code flow; refresh tokens encrypted with Fernet,
  key derived via `scrypt(PORTAL_SECRET_KEY, salt=…, n=2¹⁴, r=8, p=1)`.
- Tokens are decrypted only inside the provider, never logged, never returned
  by an endpoint.
- CSRF double-submit on all mutating routes.
- Drive folder and file names are slugified against hostile input — path
  separators, `..`, control characters and leading dots are neutralized;
  Unicode is transliterated (`Ünïcödé Tëxt` → `Unicode_Text`). Covered by
  `tests/test_drive_paths.py`.
- OAuth scopes are the narrowest that work: `drive.file` cannot read files the
  portal did not create.
- Roles are derived from data, not asserted by the client. Creating a course
  is what promotes a user to teacher, which avoids a bootstrap deadlock where
  no one can create the first course.
- In-process rate limiter keyed on client IP (240 req/min, 1200 for drafts).

---

## Scaling path

What scales on one machine, and what does not:

| Component | Single-node limit | Path beyond it |
| --- | --- | --- |
| HTTP handlers | async, ~400–700 req/s on trivial routes at 2 vCPU | more replicas behind nginx |
| Reads | WAL readers scale with pool size and cores | Postgres |
| **Writes** | **one at a time — SQLite is the hard ceiling** | Postgres (MVCC, concurrent writers) |
| Analysis | CPU-bound, 1 core's worth | more workers, or a real task queue (Celery/RQ + Redis) |
| Sessions | none — stateless cookies | nothing to do |
| Drive I/O | network-bound, already non-blocking | nothing to do |
| Upload staging | local filesystem | object store |

The repository layer, the `DriveProvider` protocol and the queue interface
exist so that these are swaps rather than rewrites. `build_engine()` already
branches on the URL and produces a correctly configured `asyncpg` engine with
`pool_pre_ping` and `pool_recycle`; the SQLite pragmas and the write gate are
skipped for it.

**The one architectural compromise to be explicit about:** the analysis queue
is in-process. Run multiple replicas and each has its own queue, so work is
partitioned by whichever replica handled the submit — that is fine, and it is
why the `Dockerfile` defaults to one worker per container. It also means a
replica that dies loses its queued jobs; the submissions themselves are
committed and `POST /api/submissions/{id}/reanalyse` recovers them. A shared
broker removes that gap.

### Measured, for honesty

The figures in `README.md` were taken on 2 vCPU / 4 GB with the load generator
sharing the CPU. The 10,000-concurrent-user target is a design goal with a
concrete path, **not a measured result** — and it cannot be measured here,
because this environment has no Postgres server and no second machine.
