# Coursework Portal

A college assignments portal that signs students in with their **own Google
account**, auto-saves their work in progress to **their own Google Drive**,
files every submission into the **teacher's Drive** in a neat folder hierarchy,
and reports a **similarity index** and an **AI-detection score** for each
submission — flagging anything at or above a configurable threshold (default
**50 %**).

Built for function, not appearance: server-rendered pages, no JavaScript build
step, no CSS framework.

---

## What it does

| Requirement | How it works |
| --- | --- |
| Direct connection to the student's Google account | OAuth 2.0 authorization-code flow with PKCE. Per-user refresh tokens are encrypted at rest (Fernet, key derived from `PORTAL_SECRET_KEY`) and used to call Drive on the student's behalf. |
| Unsubmitted work auto-saved to the student's own Drive | Drafts debounce at 1.5 s and are written to `College Coursework/<Semester>/<Course>/Drafts/` in the **student's** Drive as a single live file. Identical bytes are a no-op, so 10k students typing does not mean 10k Drive writes. Passing `snapshot: true` — or simply submitting — pins an immutable numbered copy (`…_draft_r01.txt`) alongside it, so a later edit can never rewrite what was handed in. |
| Teacher's Drive holds all submitted work, neatly organized | On submit, the file is archived into the teacher's Drive at `Submissions/<Semester>/<Course>/<Assignment>/submissions/`, named `21CS042_Meera_Iyer_A1_v1.pdf` — roll number, name, assignment, attempt. Resubmissions increment `v2`, `v3`… |
| Similarity index | Word-aligned verbatim-block matching (blake2b over 40-token windows), MinHash/LSH peer comparison (128 permutations, 32 bands), and reference-document overlap, blended `0.45 / 0.35 / 0.20`. |
| AI-detection score | A 19-feature stylometric ensemble: sentence-length burstiness, lexical diversity, repeated n-gram ratio, an AI-marker lexicon, punctuation rhythm, hedging density and more. Each feature contributes `score × weight`; the report explains which features drove the verdict. |
| Flagging at ≤ 50 % | `SIMILARITY_THRESHOLD=0.50`, `AI_DETECTION_THRESHOLD=0.50`. Either axis at or above its threshold sets `flagged=True`. |
| Modular, scalable to ~10k concurrent users | Fully async FastAPI + SQLAlchemy, stateless signed-cookie sessions (no shared session store), a bounded analysis queue with backpressure, and a repository layer that makes SQLite → Postgres a one-line config change. See `ARCHITECTURE.md`. |

Every submission also produces a machine-readable **integrity report**
(`21CS042_Meera_Iyer_A1_integrity_report.json`) and the assignment folder gets
a **`A1_roster_summary.csv`** the teacher can open in a spreadsheet.

---

## Run it in 60 seconds

No Google credentials are required. The default `DRIVE_PROVIDER=mock` is a
filesystem-backed implementation of the same interface, so the whole flow —
drafts, submissions, teacher archive, reports — runs offline.

```bash
python3.11 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt

.venv/bin/python -m scripts.seed --students 40 --assign 2 --reset
.venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Open <http://localhost:8000>, then sign in with any seeded address:

| Role | Email | Roll number |
| --- | --- | --- |
| Instructor / admin | `prof.rao@college.edu` | `STAFF01` |
| Student | `student001@college.edu` … `student040@college.edu` | `21CS001` … `21CS040` |

The seed writes 160 submissions with a handful of deliberately planted
plagiarism and AI-style samples so the flagging logic has something to find.
Development sign-in takes an email only — there is no password, and
`/auth/dev/login` returns 401 once `PORTAL_ENV=production`.

Orchestrated instead:

```bash
cp .env.example .env     # set PORTAL_SECRET_KEY and POSTGRES_PASSWORD
docker compose up -d --build
```

---

## Using real Google accounts

1. In Google Cloud Console create an OAuth **Web application** client.
2. Add `{PORTAL_BASE_URL}/auth/google/callback` as an authorized redirect URI.
3. Set in `.env`:

   ```
   GOOGLE_CLIENT_ID=…
   GOOGLE_CLIENT_SECRET=…
   GOOGLE_REDIRECT_URI=http://localhost:8000/auth/google/callback
   DRIVE_PROVIDER=google
   ```

The scopes are deliberately narrow: `openid,email,profile` plus
`drive.file` and `drive.appdata`. `drive.file` means the portal can only ever
touch files and folders **it** created in that account — it cannot read the
student's existing Drive. Keep them exactly as given; the folder layout
depends on them.

---

## Tests and checks

```bash
.venv/bin/python -m pytest tests/ -q      # 62 tests
.venv/bin/python -m ruff check app tests scripts
```

The suite covers the similarity engine, the AI detector, Drive path generation
(including hostile filenames and Unicode), the full HTTP API, an end-to-end
draft → submit → analyse → report journey, and the worker queue.

---

## Performance — measured, not asserted

On the 2 vCPU / 4 GB machine this was developed on, SQLite + WAL, mock Drive:

| Measurement | Result |
| --- | --- |
| `/healthz` throughput (concurrency 1 / 50 / 100) | 715 / 492 / 417 req/s |
| Mixed workload, 200 concurrent users | **1,024 requests, 0 failures** |
| …dashboard | p50 115 ms, p95 277 ms |
| …integrity report | p50 97 ms, p95 309 ms |
| …draft autosave | p50 1,243 ms, p95 10,437 ms |
| …submit | p50 2,354 ms, p95 15,473 ms |
| Server RSS under that load | 160 MB |

Read paths are comfortably fast at 200 concurrent users. **Writes are the
bottleneck**, and it is SQLite's single-writer model, not the application:
draft and submit latencies are dominated by serialized write transactions and
by CPU-bound analysis work competing with the request handlers.

Three caveats about these numbers, because they are easy to over-read:

- **The load generator shared the CPU with the server.** Both ran on the same
  2 vCPUs, so absolute throughput is pessimistic and noisy — a read-only run
  at 100 concurrent users measured 40 req/s with p50 770 ms, while the same
  endpoint at 200 users in the mixed run measured p50 115 ms. Treat the
  per-endpoint latencies as indicative and the *absence of errors* as the
  real result.
- **SQLite cannot be scaled past one writer.** The path to 10k concurrent
  users is Postgres plus multiple app replicas — see `DEPLOYMENT.md`. That
  path is implemented and the engine builds for `postgresql+asyncpg`, but
  **no Postgres server exists in this environment, so it is unmeasured.**
- The 10k figure is a design target, not a benchmark result.

Reproduce any of it:

```bash
.venv/bin/python -m scripts.loadtest --users 200 --duration 25 --mix mixed
.venv/bin/python -m scripts.loadtest --users 100 --duration 20 --mix read
```

---

## Layout

```
app/
  core/        config, security, crypto, logging, metrics, error mapping
  domain/      enums, SQLAlchemy entities, Pydantic schemas
  db/          async engine + session factory, repository layer
  services/
    drive/     DriveProvider protocol: mock (filesystem) and google (REST)
    similarity/  text extraction, normalization, MinHash, scoring engine
    detection/   stylometric feature extraction + ensemble model
    workers/     bounded queue, CPU thread pool, analysis pipeline
  api/         JSON endpoints, split by resource
  web/         Jinja2 server-rendered pages
scripts/       seed.py (demo data), loadtest.py (load harness)
tests/         62 tests
deploy/        nginx.conf
ARCHITECTURE.md   design, concurrency model, scaling path
DEPLOYMENT.md     how to actually run this in production
```

## HTTP surface

Verified against the live route table (`GET /openapi.json`).

```
/healthz    liveness, no dependencies (not in the OpenAPI spec — raw route)

/auth       GET  /google/login      GET  /google/callback
            POST /dev/login         POST /logout            GET /me

/api/courses
            GET  /                  POST /
            POST /{id}/enrol        POST /{id}/enrol/{email}
            GET  /{id}/roster       POST /{id}/reference    GET /{id}/reference
            POST /{id}/validate-code

/api/assignments
            GET  /                  POST /                  GET /{id}
            GET  /{id}/submissions  GET  /{id}/summary
            POST /{id}/draft        GET  /{id}/draft        GET  /{id}/draft/content
            POST /{id}/draft/upload
            POST /{id}/submit       POST /{id}/submit-draft
            GET  /{id}/my-submission
            GET  /{id}/drive-location          (teacher only)

/api/submissions
            GET  /{id}              GET  /{id}/report       GET /{id}/download
            POST /{id}/grade        POST /{id}/reanalyse

/api/drive  GET  /status  /layout  /tree  /tree/mine
/api/system GET  /health  /queue   /metrics  /config

pages       GET  /  /login  /dashboard  /courses  /drive  /system
            GET  /assignment/{id}  /assignment/{id}/review
```
