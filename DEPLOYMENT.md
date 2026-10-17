# Deployment

Three ways to run this, from smallest to largest.

---

## 1. Single process, SQLite — a department or a class

Fine for up to a few hundred concurrent students. One machine, no external
services.

```bash
python3.11 -m venv .venv
.venv/bin/pip install -r requirements.txt

cp .env.example .env
# set PORTAL_SECRET_KEY:  python -c "import secrets;print(secrets.token_urlsafe(48))"

.venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 1
```

**Use `--workers 1`.** With SQLite, extra processes multiply write contention
against the single writer and each gets its own analysis queue. Vertical
scaling here means more cores for analysis, not more workers.

Point nginx or Caddy at it for TLS:

```nginx
location / {
    proxy_pass http://127.0.0.1:8000;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_read_timeout 300s;          # submissions upload to Drive in-request
    client_max_body_size 32m;         # matches MAX_UPLOAD_BYTES (25 MB)
}
```

**Back up `data/`.** It holds `portal.db` (users, submissions, reports,
encrypted OAuth tokens) and `uploads/` (staged files). Stop the app first, or
copy `portal.db` together with `portal.db-wal` and `portal.db-shm` — with WAL
active, the `-wal` file contains committed data that is not yet in the main
database.

---

## 2. Docker Compose — Postgres, horizontally scaled

```bash
cp .env.example .env
# required:
#   PORTAL_SECRET_KEY    shared by every replica
#   POSTGRES_PASSWORD
#   GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET / GOOGLE_REDIRECT_URI

docker compose up -d --build
docker compose up -d --scale portal=4      # add replicas
docker compose logs -f portal
```

Services: `nginx` → `portal×N` → `postgres`. Sessions are stateless signed
cookies, so no session store and no sticky sessions are needed.

Tables are created by SQLAlchemy on startup (`init_models()`). There are no
migrations yet — for a schema change on an existing deployment, take a
`pg_dump` first.

Scaling rules of thumb, per replica:

| Setting | Suggested | Why |
| --- | --- | --- |
| `ANALYSIS_WORKERS` | 1–2 | one queue consumer per replica |
| `ANALYSIS_CPU_THREADS` | = container CPU limit | analysis is CPU-bound |
| `DB_POOL_SIZE` | 20 | `replicas × (pool + overflow)` must stay under Postgres `max_connections` |
| `DB_MAX_OVERFLOW` | 20 | |
| CPU limit | 2.0 | leaves headroom for request handling |

The one shared piece of state beyond Postgres is `data/uploads`, where files
are staged before they reach Drive. With more than one replica that volume
must be shared storage (NFS, EFS, a network mount) or replaced by an object
store — otherwise a submission staged on replica A cannot be found by
replica B.

**This compose file could not be run in the environment it was written in** —
there is no `docker` binary available here. It is reviewed and consistent with
the settings the app actually reads, but it has not been executed.

---

## 3. Production checklist

**Before go-live**

- [ ] `PORTAL_ENV=production` — this disables `/auth/dev/login`, which
      otherwise signs in anyone who supplies an email.
- [ ] `PORTAL_SECRET_KEY` set to a strong random value. Rotating it
      invalidates every session **and** makes every stored Google OAuth token
      undecryptable, forcing every student to reconnect.
- [ ] TLS terminated at the proxy; uvicorn started with `--proxy-headers
      --forwarded-allow-ips=<proxy CIDR>` so the rate limiter sees real client
      IPs rather than the proxy's.
- [ ] `PORTAL_BASE_URL` matches the public origin, and
      `{PORTAL_BASE_URL}/auth/google/callback` is registered in Google Cloud
      Console.
- [ ] Real credentials in place: `DRIVE_PROVIDER=google`, plus the
      institution's own account (`INSTITUTION_EMAIL`,
      `INSTITUTION_REFRESH_TOKEN`) so submissions still land in a teacher's
      Drive when a course has no instructor linked.
- [ ] Automated backups of the database **and** of the Drive archive.
- [ ] Monitoring wired to `/healthz` (liveness) and `/api/system/health`
      (database + queue depth).

**Watch these**

| Endpoint | What it tells you |
| --- | --- |
| `/healthz` | liveness, no dependencies |
| `/api/system/health` | database reachable, analysis queue depth |
| `/api/system/queue` | `depth` climbing means analysis cannot keep up — add workers |
| `/api/system/metrics` | request counts, latency percentiles, `db_pool_checked_out` |

`db_pool_checked_out` sitting at the pool ceiling is the early warning that
you have hit the write serialization limit and need Postgres.

A sustained rate of `503` responses with `Retry-After` is deliberate
backpressure, not a failure. It means the server is shedding load rather than
collapsing.

---

## Configuration reference

Everything is read from the environment (or `.env`), prefixed `PORTAL_` for
core settings and unprefixed for feature settings.

| Variable | Default | Notes |
| --- | --- | --- |
| `PORTAL_ENV` | `development` | `production` disables dev sign-in |
| `PORTAL_SECRET_KEY` | insecure placeholder | **must** be changed |
| `PORTAL_DATABASE_URL` | `sqlite+aiosqlite:///./data/portal.db` | `postgresql+asyncpg://…` for production |
| `PORTAL_BASE_URL` | `http://localhost:8000` | must match the public origin |
| `DRIVE_PROVIDER` | `mock` | `google` for real Drive |
| `GOOGLE_CLIENT_ID` / `_SECRET` | empty | from Google Cloud Console |
| `TEACHER_DRIVE_ROOT_ID` | `root` | parent folder in the institution account |
| `INSTITUTION_EMAIL_DOMAIN` | `college.edu` | who may create a course |
| `ALLOW_OPEN_COURSE_CREATION` | `true` | set `false` once the first courses exist |
| `SIMILARITY_THRESHOLD` | `0.50` | flag at or above |
| `AI_DETECTION_THRESHOLD` | `0.50` | flag at or above |
| `AI_DETECTION_MIN_WORDS` | `120` | below this, `reliable=false` |
| `ANALYSIS_WORKERS` | `2` | queue consumers |
| `ANALYSIS_CPU_THREADS` | `2` | size the CPU pool to cores |
| `ANALYSIS_QUEUE_CAPACITY` | `5000` | beyond this, 503 + `Retry-After` |
| `DB_POOL_SIZE` / `DB_MAX_OVERFLOW` | `20` / `40` | |
| `MAX_UPLOAD_BYTES` | `26214400` | 25 MB |
| `RATE_LIMIT_PER_MINUTE` | `240` | `1200` for draft autosaves |
| `DRAFT_DEBOUNCE_MS` | `1500` | client-side autosave interval |

Run `/api/system/config` while authenticated to see the effective values.

---

## Operational notes

**Recovering a stuck analysis.** Submissions are committed before analysis is
enqueued, so an interrupted job never loses work:

```bash
curl -X POST -H "Cookie: cp_session=…" \
     -H "X-CSRF-Token: …" \
     http://host/api/submissions/1234/reanalyse
```

**Changing thresholds** takes effect on the next analysis; existing verdicts
are not recomputed. Re-run `/reanalyse` for assignments you want re-scored.

**A student's Drive got disconnected.** This is a known gap, stated plainly
rather than papered over: `save_draft` calls Drive directly, and if that call
fails the autosave fails with an error — there is **no** local-staging fallback
for drafts, and nothing in the app flips `drive_linked` to `false` on a revoked
token. The student's browser keeps the text in the editor, so the work is not
lost until they close the tab, but it is not durable server-side. The
*submission* path is better behaved: the row is committed before the upload,
and `archive_account()` falls back to the institution Drive
(`ArchiveResult.degraded=True`) when no instructor Drive is linked. Closing the
draft gap means staging to `data/uploads` on Drive failure and retrying — a
small change, deliberately not made here.

**Postgres connection limits.** `replicas × (DB_POOL_SIZE + DB_MAX_OVERFLOW)`
must stay below `max_connections`. Four replicas at the defaults need 160.
