# CLAUDE.md — Lexa build instructions

This file is read automatically by Claude Code. It carries the operational rules the design doc
(`DESIGN.md`) doesn't: pinned stack, conventions, invariants, commands, and the current task slice.

**Always read `DESIGN.md` first.** It is the source of truth for *what* and *why*. This file governs
*how we build it* and *what not to break*.

---

## 0. How to work in this repo (read every session)

- **Build in the slice you're given, nothing more.** Do not implement future phases or features not in the
  current task, even if they seem trivial. Scope creep across sessions is the main failure mode.
- **Stop at the review gate** named in each task. End your turn, summarize what you built, and wait. Do not
  continue into the next task.
- **When a decision isn't pinned here or in `DESIGN.md`, ask — don't guess and proceed.** Inconsistent
  choices across sessions (a different chunk size, a different model) are expensive to undo.
- **Never silently change a schema, an interface, or an invariant below.** If a task seems to require it,
  stop and flag it.
- Prefer small, reviewable commits with clear messages over one large drop.
- Write the test alongside the code, not after. A slice isn't done until its tests pass.

---

## 1. Hard invariants — DO NOT violate without flagging

These are load-bearing. Breaking them causes rewrites later.

1. **Provenance is captured at parse time and never reconstructed.** Every chunk carries `page_start`,
   `page_end`, `bboxes` (a **list** of normalized `{page,[x0,y0,x1,y1]}`), `char_start`/`char_end`,
   `section_path`. This ships complete in Phase 1 even though highlights don't render until Phase 2. Do
   not defer any provenance field.
2. **Bounding boxes are normalized to 0–1** (fraction of page dimensions), never raw points/pixels, at
   every layer through to the browser.
3. **Parse once into the immutable IR; downstream stages are pure transforms over it.** Chunking and
   embedding must be re-runnable without re-parsing. Never fold parsing logic into the chunker.
4. **LLM and embedding access goes through the provider abstraction** (`providers/`). No direct vendor SDK
   calls anywhere else in the codebase.
5. **The model never emits a citation — only an `[S#]` reference to a server-provided source.** Page
   numbers/bboxes are always resolved server-side from chunk metadata, never produced by the LLM.
6. **One shared entailment/grounding module** (Phase 2) is used by both the request path and the eval
   harness. Never fork it.
7. **Every query is tenant-scoped by `user_id`** at the DB layer. No query may return another tenant's
   data.
8. **Chunks are partitioned by `document_id`**; single-doc retrieval must never scan the whole corpus.

If a task appears to require breaking one of these, **stop and explain the conflict.**

---

## 2. Stack (pin exact versions at scaffold time, then lock)

Resolve to current stable versions when scaffolding, write them into `pyproject.toml` / `package.json`,
commit the lockfiles, and **do not bump versions without flagging**.

**Backend**
- Python 3.12+, FastAPI, Uvicorn, Pydantic v2
- SQLAlchemy 2.x (async), Alembic for migrations
- Postgres 16+ with the **pgvector** extension
- Async jobs: **Celery + Redis** for Phase 1 (revisit Temporal in Phase 2 if durability demands it — do
  not introduce Temporal yet)
- PDF parse: **PyMuPDF (fitz)** as primary; keep the parser behind an interface so an `unstructured`
  fallback can slot in
- HTTP/test: httpx, pytest, pytest-asyncio

**Frontend**
- Next.js (App Router) + TypeScript + Tailwind
- PDF.js for the viewer (Phase 1 renders pages; Phase 2 adds the overlay layer — pick the viewer now so
  it isn't replaced)
- SSE for streaming (no WebSockets unless flagged)

**Infra (local dev)**
- Docker Compose: Postgres+pgvector, Redis
- S3 access via an interface with a **local MinIO / filesystem adapter** for dev

If a library choice isn't specified, propose it and wait for confirmation before adding it.

---

## 2.1 Pinned decisions (settled at scaffold review — do not re-decide)

These resolve ambiguities found in the pre-scaffold design review. Changing any of them is a
flag-and-discuss event, same as the invariants in §1.

1. **Auth (Phase 1):** minimal real auth ships in Phase 1 — register/login endpoints, **argon2id**
   password hashing, **JWT access tokens (HS256, 60-minute expiry, no refresh tokens in P1)**,
   `get_current_user` FastAPI dependency. Tenant scoping is enforced **app-layer**: every service
   query filters by the authenticated `user_id`. **Postgres RLS is a flagged Phase 2 hardening
   task** — defense-in-depth on top of app-layer scoping, never a replacement for it.
2. **Upload finalize:** direct-to-storage presigned upload is completed by an explicit
   `POST /documents/{id}/complete` server-side step: `HeadObject` size enforcement (delete
   oversize objects), magic-byte validation, SHA-256 hash — all **before** any job is enqueued.
   S3 keys are tenant-prefixed (`users/{user_id}/…`) and finalize verifies caller ownership.
   Malware scanning is **explicitly deferred** (documented gap; ClamAV slots in at finalize later).
3. **Dedupe:** SHA-256 dedupe is scoped **per `user_id`** (`UNIQUE (user_id, doc_hash)`), never
   corpus-wide — global dedupe is a cross-tenant existence side channel.
4. **Chunk partitioning mechanics:** `chunks` is partitioned by **HASH (document_id), 16
   partitions** (pinned — effectively immutable after creation). This forces composite keys:
   PK `(id, document_id)`; parent-child self-FK `(parent_chunk_id, document_id) → (id, document_id)`.
   pgvector **>= 0.8** (iterative index scans) for filtered HNSW recall.
5. **Embedding provider pin:** OpenAI `text-embedding-3-small`, **`vector(1536)`**. The dimension
   is baked into the schema; switching to a different-dimension model is a migration + full
   re-embed (`embed_model`/`embed_version` per chunk make re-embedding selective). The provider
   abstraction (§1 invariant 4) makes the vendor swappable without code changes elsewhere.
6. **LLM provider pin:** Anthropic (official `anthropic` Python SDK), default model
   **`claude-opus-4-8`**, configurable via settings. All access through `providers/llm/`.
7. **Worker DB pattern:** Celery workers use a **separate synchronous SQLAlchemy engine/session
   factory**. The API's async engine is never shared with workers. (This is the one sanctioned
   exception to the "all I/O is async" convention in §3.)
8. **Status-pipeline safety:** document status transitions are **guarded**
   (`UPDATE … WHERE status = expected` — a stale transition is a no-op); every pipeline stage is
   idempotent-by-checkpoint; `ingestion_jobs` has `UNIQUE (document_id, stage)`. Celery runs with
   `acks_late=True`, task `time_limit`/`soft_time_limit`, and `max_memory_per_child` (untrusted-PDF
   hardening — a decompression bomb kills one task, not the worker fleet).
9. **SSE progress:** the progress endpoint always sends a **DB snapshot of current status first,
   then subscribes** to Redis pub/sub for live deltas (pub/sub alone loses events across
   reconnects).
10. **Lexical search:** Postgres `tsvector`/`ts_rank` (BM25-approximate — say "lexical search",
    not "BM25", in code and docs). Two configs indexed: **`english`** (stemmed) and **`simple`**
    (exact — defined terms and clause numbers must not be stemmed). RRF consumes ranks only, so
    this never affects fusion.
11. **API hardening in P1 scope:** CORS allowlist, request body-size caps, and basic rate
    limiting are Task 1/3 scope, not "later".
12. **CI from Task 1:** a GitHub Actions workflow runs `ruff`, `mypy`, and `pytest` on every push;
    the §7 definition of done is enforced by CI, not just convention.

---

## 3. Conventions

- **Folder structure** (backend):
  ```
  app/
    api/        # routers only — thin, no business logic
    core/       # config, security, deps, exceptions, logging
    services/   # ingestion/  retrieval/  generation/
    models/     # SQLAlchemy
    schemas/    # Pydantic DTOs (incl. the immutable BlockIR/PageIR shapes)
    workers/    # Celery app + tasks
    providers/  # llm/  embeddings/  — vendor abstraction
    parsers/    # PDF parser interface + PyMuPDF impl (kept out of services/ to
                #   enforce invariant 3 — parsing never folds into the chunker)
    storage/    # object-storage interface + S3/MinIO + local adapters
    db/         # session, Alembic
  tests/
  ```
- Routers contain **no logic** — they validate input and call a service.
- All I/O in the **API path** is **async**; no blocking calls in request handlers. **Celery workers
  are synchronous** and use their own sync SQLAlchemy session factory (§2.1 #7) — never the API's
  async engine.
- Pydantic schemas (`schemas/`) are the API contract; never return raw ORM objects.
- Errors: explicit exception types + a single handler mapping them to HTTP responses. No bare `except`.
- Config via environment, validated by a Pydantic `Settings` object. No hardcoded secrets, no secrets in
  committed files.
- Type hints everywhere; the build runs a type checker (mypy/pyright) and it must pass.
- Naming: `snake_case` Python, `camelCase` TS, table names plural.

---

## 4. Commands

> Fill these in at scaffold time and keep them current — Claude Code relies on them.

```
# env
docker compose up -d            # Postgres+pgvector, Redis, MinIO
cp .env.example .env

# backend
uv sync                         # or poetry install
alembic upgrade head            # apply migrations
uvicorn app.main:app --reload
celery -A app.workers worker -l info

# tests / quality
pytest
mypy app/                       # type check — must pass
ruff check .                    # lint

# frontend
cd frontend && pnpm install && pnpm dev
```

**Migrations:** every schema change is an Alembic migration. Never edit the DB by hand. Never edit a
migration that's already been applied/committed — add a new one.

---

## 5. Current phase: Phase 1 — MVP

**Goal:** upload a large PDF → ask questions → get answers that cite a page. One document, one chat.
See `DESIGN.md` §4, §5, §8, §11.

Build the tasks below **in order**, stopping at each review gate. Do not start a task until the previous
one is reviewed.

### Task 1 — Scaffold + foundations
Repo skeleton (§3 structure), Docker Compose (Postgres+pgvector >= 0.8, Redis, MinIO), `.env.example`,
Pydantic `Settings`, async SQLAlchemy session (API) **+ sync session factory (workers, §2.1 #7)**,
Alembic initialized, **provider abstraction interfaces** for LLM and embeddings (interface + one
concrete impl per §2.1 #5/#6, behind config), storage interface + S3/MinIO and local adapters
(tenant-prefixed key scheme), Celery app wired to Redis with the §2.1 #8 hardening settings, health
endpoint, structured logging with request IDs, CORS + body-size-cap middleware, **auth primitives**
(argon2id hash/verify + JWT encode/decode per §2.1 #1 — endpoints come with Task 3), **GitHub Actions
CI** (ruff, mypy, pytest), pytest wired up.
**Skip:** any business logic. **Gate:** stop after `docker compose up`, `alembic upgrade head`, and a
passing health-check test work. Summarize the layout.

### Task 2 — Schema + migrations
Implement the Phase-1 tables from `DESIGN.md` §8: `users`, `documents` (with `size_bytes`,
`deleted_at`, `UNIQUE (user_id, doc_hash)`), `sections`, `chunks` (full provenance fields,
`vector(1536)`, tsvector in `english` + `simple` configs, **hash-partitioned by `document_id`, 16
partitions, composite PK/FK per §2.1 #4**), `ingestion_jobs` (`UNIQUE (document_id, stage)`), `chats`,
`messages` (index on `(chat_id, created_at)`). `created_at`/`updated_at` on all tables. One Alembic
migration chain. HNSW index on `embedding`, GIN on the tsvectors.
**Skip:** eval and comparison tables (later phases). **Gate:** migrations apply cleanly up and down;
print the resulting schema.

### Task 3 — Upload + validate + triage
**Auth endpoints first** (register/login per §2.1 #1; every later endpoint requires
`get_current_user`). Presigned-style multipart upload via the storage interface, then the explicit
**finalize step** (`POST /documents/{id}/complete`, §2.1 #2): server-side size enforcement, magic-byte
validation, SHA-256 hash, **per-user dedupe** (§2.1 #3), caller-ownership check on the tenant-prefixed
key — only then the **validate/triage stage** (open test, encryption check, page-count ceiling,
per-page text-layer probe → page manifest `text|scanned|mixed`) with the §2.1 #8 Celery hardening.
Enqueue an ingestion job, return `202 + job_id`. Basic rate limiting on the upload endpoints.
**Mock:** OCR path — flag `scanned` pages and, for Phase 1, reject docs that need OCR with a clear
message. Malware scan: deferred (§2.1 #2). **Gate:** uploading a PDF persists a `documents` row and a
queued job; manifest is stored. Stop before parsing.

### Task 4 — Parse → IR (page-parallel)
PyMuPDF parser behind the parser interface producing `BlockIR` per page (type, text, **normalized bbox**,
reading-order index, font). Multi-column reading-order reconstruction. Tables kept atomic. Persist PageIR
JSON to storage (parse-once artifact) and a canonical linearized text stream for global char offsets.
Page-parallel via Celery.
**Gate:** a 100+ page PDF parses to IR with correct page numbers and normalized bboxes spot-checked. Stop
before section detection.

### Task 5 — Section detection + chunking
Heading detection (font-vs-body-baseline + numbering grammar) → section tree + `section_path` per block.
Then **parent-child chunking**: parents = sections (capped), children = ~300-token sub-chunks (pin exact
size with the embedder's tokenizer — propose and confirm), no cross-section splits, ~12% overlap, tables
atomic. **Write complete provenance per chunk** (§8, invariant 1).
**Gate:** chunks exist with full provenance; verify no chunk crosses a section boundary and every chunk
has a non-empty `bboxes` list. Stop before embedding.

### Task 6 — Embedding + indexing
Batched embedding through the provider abstraction (dedupe by content hash, store
`embed_model`/`embed_version` per chunk), token-bucket rate limit, write chunks+embeddings
transactionally, build/refresh HNSW + tsvector. Flip document to `READY` only after all batches commit.
**Gate:** a full document ingests end-to-end to `READY`; vector + text search both return rows. Stop
before retrieval.

### Task 7 — Hybrid retrieval + RAG answer
Dense ANN ∥ BM25, **RRF fusion**, retrieve children then expand to parents (Phase 1 may skip rerank — use
fused order). Build the prompt with ephemeral `[S#]` source IDs, document fenced as untrusted data,
structured per-claim output. **Structural citation validation** (drop any `[S#]` not in the request set).
**Page-level** citation resolution (bbox highlights are Phase 2). Stream the answer; persist to
`messages`. **Mock:** reranker, entailment verification. **Gate:** ask a question, get a streamed answer
citing a page, with fabricated IDs stripped.

### Task 8 — Frontend MVP
Upload + progress (coarse stage enum), document list, **PDF.js viewer** (render pages — this viewer is
kept and extended in Phase 2), chat pane, streamed answer with a clickable citation that **jumps to the
cited page**.
**Gate:** full demo path works in the browser: upload → ready → ask → answer → click citation → viewer
jumps to the page.

---

## 6. What is explicitly OUT of scope until later phases

Do not build, even partially: reranking, entailment-based citation verification, bbox highlight overlays,
confidence/abstain logic, OCR fallback, the evaluation harness, the semantic comparison engine, risk
scoring, extraction, multi-doc reasoning, the dashboard, Temporal. These have dependencies that aren't
ready; building them now creates rework. If a task seems to need one, stop and flag it.

---

## 7. Definition of done (per task)

A task is complete when: code follows §3 conventions; the provenance/invariant rules in §1 hold; tests
for the slice pass; `mypy` and `ruff` are clean; the review gate's demonstrable outcome works; and you've
stopped and summarized. Then wait.
