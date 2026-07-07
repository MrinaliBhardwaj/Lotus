# Lexa — Large Document Intelligence Platform

Upload large PDFs (contracts, research papers, financial reports) and interact with them:
citation-grounded Q&A, summaries, clause/risk extraction, and meaning-level diffs between two
versions — at 500–1000 page scale.

- **What & why:** [`DESIGN.md`](DESIGN.md)
- **How we build it (invariants, pinned decisions, task plan):** [`CLAUDE.md`](CLAUDE.md)

## Status

Phase 1 in progress. **Task 1 (scaffold + foundations) complete:** config system, DI, async
SQLAlchemy + Alembic, Celery + Redis, S3/MinIO + local storage adapters, LLM/embedding provider
abstraction (Anthropic / OpenAI pinned, fakes for tests), auth primitives (argon2id + JWT),
structured logging with request IDs, CORS + body-size middleware, health endpoint, CI.

## Quickstart

```bash
# infra: Postgres 16 + pgvector, Redis, MinIO (bucket auto-created)
docker compose up -d
cp .env.example .env

# backend
uv sync
uv run alembic upgrade head
uv run uvicorn app.main:app --reload

# worker
uv run celery -A app.workers worker -l info

# quality gate (same as CI)
uv run ruff check .
uv run mypy app/
uv run pytest
```

`GET http://localhost:8000/health` → `{"status":"ok","app":"lexa","env":"dev"}`

## Layout

```
app/
  api/        # routers only — thin, no business logic
  core/       # config, security, deps, exceptions, middleware, logging
  services/   # ingestion/ retrieval/ generation/ (business logic)
  models/     # SQLAlchemy ORM (Task 2)
  schemas/    # Pydantic DTOs — the API contract
  workers/    # Celery app + tasks
  providers/  # llm/ embeddings/ — the ONLY place vendor SDKs are touched
  parsers/    # PDF parser interface + PyMuPDF impl (Task 4)
  storage/    # object-storage interface + S3/MinIO + local adapters
  db/         # engines/sessions + Alembic migrations
tests/
```
