# Lexa — Large Document Intelligence Platform

**Technical Design Document**

> Upload large PDFs (contracts, research papers, financial reports) and interact with them: ask
> citation-grounded questions, generate summaries, extract clauses/risks, and run meaning-level
> diffs between two versions — at 500–1000 page scale.

> **Status:** design complete; phased build in progress. *(Name note: settle on one of Lexa / Lotus
> before this ships — used consistently as "Lexa" throughout this doc.)*

---

## 1. What this is, and why it's hard

A naive "RAG over PDFs" demo is a weekend project. Three requirements push Lexa into senior-engineering
territory, and the entire architecture is organized around them:

1. **Citation precision** — every answer points to the exact paragraph (page + bounding box), and the
   citation is *verified to actually support the claim*, not merely produced by the model.
2. **Semantic comparison** — detect that "liability cap raised $1M → $5M" while ignoring pure rewording.
   Naive text diff fails on both halves: it flags equivalent rewordings and misses meaning changes hidden
   in similar wording.
3. **Genuinely large documents** — a 1000-page filing must ingest without timing out, and queries must
   stay fast within it.

Most tutorials quietly break on all three. Lexa is designed so that none of the hard features are
retrofits — provenance, durability, and measurability are built in from the first commit.

---

## 2. Architecture at a glance

Two planes, kept strictly separate in code and in reasoning:

**Ingestion plane** (async, write-heavy, durable):

```
Upload → S3 → enqueue → [Validate → Parse→IR → OCR-fallback → Structure →
Chunk → Embed → Index] → READY → progress events
```

Each stage is a discrete, idempotent, retryable step with persisted state. The user watches a real
progress bar driven by completed work units, not a fake timer.

**Query plane** (sync/streaming, read-heavy):

```
Question → Hybrid retrieve (BM25 + dense) → Rerank → Parent expansion →
Grounded LLM answer (stream) → Entailment-verify citations → Resolve to page+bbox
```

**The invariant that ties the planes together:** provenance flows end to end. Every chunk carries
`page`, normalized `bbox` list, global `char` offsets, and `section_path` from parse time onward. Lose
this at parsing and exact citations become impossible to add later.

---

## 3. Tech stack (opinionated)

| Layer | Choice | Why |
|---|---|---|
| API | Python + FastAPI | Async-native; needed for streaming + concurrent ingestion |
| Async jobs | Temporal (or Celery+Redis to start) | Durable, resumable multi-stage pipelines |
| Vector store | **pgvector** (in Postgres) | Embeddings + metadata + relational data transactionally consistent in one store; partition by `document_id` |
| Relational DB | Postgres | Same instance: users, docs, chats, jobs, eval |
| PDF parsing | PyMuPDF / `unstructured` (+ OCR fallback) | Layout-aware with coordinates — never PyPDF2 |
| LLM orchestration | LlamaIndex / thin custom layer | Keep retrieval logic explicit and controllable |
| Provider | **Abstracted behind an interface** | Swap embedding/LLM vendors without a rewrite |
| Frontend | Next.js (App Router) + TS + Tailwind + PDF.js | Streaming answers, zoom-safe highlight overlays |
| Object storage | S3 / R2 | Raw PDFs, parsed IR, lazily-rendered page images |

**Three decisions made on day one to avoid rewrites later:**

- **Full provenance schema from the first chunk** (even before highlights render). Adding it later means
  re-parsing every document.
- **Vendor abstraction for LLM + embeddings** from commit one.
- **One shared entailment/grounding module** used by *both* the runtime citation path and the eval
  harness — never forked.

---

## 4. The ingestion pipeline

Built on one principle: **parse once into an immutable Intermediate Representation (IR), then treat every
downstream stage as a pure transform over that IR.** This buys two production properties: you can
re-chunk or re-embed a 1000-page doc *without re-parsing or re-OCR'ing it*, and any stage resumes from
its predecessor's persisted output after a crash. Large docs are parsed **page-parallel** — a page is an
independent unit of work — which is what keeps a 1000-page contract from timing out.

**Stages:**

1. **Upload** — presigned S3 multipart (resumable, browser→storage direct, API stays stateless),
   followed by an explicit server-side **finalize step** (`POST /documents/{id}/complete`): because the
   API never sees the bytes during a direct-to-storage upload, size enforcement (`HeadObject`, abort +
   delete oversize objects), magic-byte validation, and the SHA-256 content hash all happen at finalize,
   *before* any job is enqueued. Object keys are tenant-prefixed (`users/{user_id}/…`) and finalize
   verifies the key belongs to the caller. **Dedupe by SHA-256 is scoped per `user_id`, never
   corpus-wide** — global dedupe would leak the existence of another tenant's document.
2. **Validate & triage** — open test, encryption check, page-count ceiling, and a per-page text-layer
   probe producing a **page manifest** (`text` / `scanned` / `mixed`). This is what lets you OCR only the
   12 scanned exhibit pages of a 600-page filing.
3. **Parse → IR** — per-page `BlockIR` (type, text, normalized bbox, reading-order index, font, OCR
   confidence). Three details that matter: **bboxes normalized to 0–1** (resolution-independent
   highlights later), **multi-column reading-order reconstruction** (naive top-to-bottom corrupts
   two-column papers), **tables kept atomic**.
4. **OCR fallback** — only on flagged pages; rasterize ~300 DPI, word-level geometry mapped into the
   *same* `BlockIR` schema so downstream stages never know the source. Confidence quality-gate.
5. **Section detection** — combine font-size-vs-body-baseline, weight/centering/whitespace, and numbering
   grammar (`3.2.1`, `Article IV`) into a heading tree. Each block inherits a `section_path` breadcrumb.
6. **Parent-child chunking** — **parent** = logical section (context); **child** = ~200–400 token
   sub-chunk (the embedded/retrieved unit). Never split across section boundaries; token-sized with the
   embedder's own tokenizer; ~10–15% overlap; tables atomic.
7. **Per-chunk provenance** (the crown jewel — see schema §8). Key non-obvious field: `bboxes` is a
   **list** — a chunk's footprint is a union of rects across possibly multiple pages.
8. **Embedding** — batched to provider max, deduped by content hash, token-bucket rate-limited, with
   `embed_model`/`embed_version` stored per chunk for selective re-embedding.
9. **Storage** — raw PDF + IR JSON + lazily-rendered page images in S3; chunks/sections in Postgres;
   HNSW + tsvector indexes in pgvector, partitioned by `document_id`. Doc flips to `READY` only after all
   batches commit.
10. **Failure recovery** — idempotent content-addressed stages, page-level checkpointing (never re-OCR
    994 good pages for 6 failures), transient-vs-permanent retry classification, soft-fail `DEGRADED`
    mode, dead-letter queue, heartbeat watchdog.
11. **Progress** — status enum with sub-counters (`pages_parsed/total`, `chunks_embedded/total`)
    published via Redis pub/sub → SSE. Pub/sub is fire-and-forget, so the SSE endpoint always sends a
    **DB snapshot of current status first, then subscribes for live deltas** — a client that connects
    late or reconnects never misses the terminal `READY`/`FAILED` event.

---

## 5. The citation system

**The principle that makes it Perplexity-grade: the LLM never emits a citation, only a reference to an ID
you handed it.** Page numbers and bounding boxes are deterministic metadata you own. The model's only job
is "which of these N provided sources support this claim" — a constrained selection over a closed set.
Every ID is validated against that set before the user sees it, so hallucinated page numbers are
structurally impossible.

**Retrieval → answer flow:**

- **Hybrid retrieval** — dense ANN (HNSW, scoped to the doc) ∥ **lexical search via Postgres `tsvector`**
  (`ts_rank` — a BM25-*approximate* ranking, not true BM25; a real-BM25 engine like ParadeDB/pg_search is
  the upgrade path if lexical quality becomes the bottleneck). Lexical search is not optional for
  legal/financial docs — it catches clause numbers, defined terms, dollar figures that semantic
  similarity blurs. Two text-search configs are indexed: `english` (stemmed) and `simple` (exact — defined
  terms and clause numbers must not be stemmed away). Fuse with **Reciprocal Rank Fusion** (rank is the
  only honest common currency between incomparable score scales — and RRF consumes only ranks, so the
  ts_rank-vs-BM25 distinction never contaminates fusion).
- **Rerank** — cross-encoder over the fused top ~40 → top ~8. The single largest quality lever; the
  bi-encoder's top-1 is frequently not the best passage.
- **Parent expansion** — *after* rerank (reranking on fat parents reintroduces the noise child
  granularity removed). Model reads parent; citation highlights child.
- **Prompting** — sources labeled with ephemeral request-scoped IDs `[S1]…[S9]` (not UUIDs); document
  fenced as untrusted data (prompt-injection defense); structured per-claim output `{text, citations:[]}`.
- **Anti-hallucination, three layers:** (a) deterministic ID-set validation drops fabricated IDs;
  (b) **entailment verification** — an NLI check that each cited source *actually supports* its claim,
  catching real-ID/wrong-claim citations; (c) **grounding ratio** = fraction of claims with ≥1 entailed
  citation, gating the abstain path.
- **Resolution** — `[S#]` → chunk_id → provenance; merge overlapping rects per page, group by page for
  multi-page highlights; coordinates stay **normalized to the browser**.
- **Rendering** — PDF.js canvas + separate absolutely-positioned overlay layer; zoom-safe because
  `pixel = norm × currentPageDim` recomputes on every zoom with no re-fetch; click chip → scroll +
  pulse highlight.
- **Confidence** — three independent signals (retrieval score+margin, per-claim entailment, answer
  grounding ratio). **"Insufficient evidence" abstention is a feature** — for contracts, a confident wrong
  answer is the catastrophic outcome.

---

## 6. The semantic comparison engine

A semantic diff is two problems in sequence: **first alignment ("what corresponds to what"), then
change-classification ("how did the meaning move") — only on aligned pairs.** Tractable at 500+ pages
because **alignment cascades hierarchically** (sections first, then clauses only *within* aligned section
pairs — block-diagonal, never O(n²) across the whole doc) and **reuses ingestion embeddings**.

- **Section alignment** — composite embeddings (`normalized_title + section_summary`, never title-only),
  thresholded Hungarian assignment (unmatched = add/delete, a valid outcome), reordering handled for free
  by embedding match, split/merge detected by cardinality.
- **Clause alignment** — tiered cosine gates: >0.95 unchanged; 0.78–0.95 candidate-changed (the only band
  that costs an LLM call); <0.78 addition/deletion.
- **Semantic diff** — **bidirectional entailment** separates paraphrase from real change: A⇄B equivalent →
  cosmetic, discard; one-directional → narrowed/broadened; neither → genuine change, classified into a
  **typed delta** `{change_type, old_value, new_value, direction, affected_party, citations}`.
- **Contradiction/supersession** — deterministic amendment grammar ("supersedes", "deleted in its
  entirety") + NLI contradiction, both across docs and *within* one doc (internal inconsistency = red
  flag).
- **Structural changes** — moved/merged/split/added/removed derived purely from alignment cardinality.
- **Risk scoring** — rule-based category prior (liability/indemnity/termination = high) × LLM impact pass
  with a **mandatory rationale**; framed as "flagged for review," not legal advice.

---

## 7. The evaluation harness

The piece that most elevates perceived seniority — it proves you *measure* quality rather than just build
features. Two principles: **every component is evaluated in isolation so a regression is attributable to a
stage**, and **the golden dataset is the product** (labels are the expensive part, not metric formulas).

- **Golden set** — chunk-anchored generation (the anchor becomes the free source label) +
  **reverse-validation** (a separate model must answer from the question alone; discard ambiguous /
  general-knowledge / unanswerable) + ~10–15% human audit. **Set-valued source labels** (a question can
  have multiple supporting chunks). Deliberate question taxonomy: factoid, multi-hop, table/numeric,
  **negative/unanswerable** (the only way to measure the abstain path), ambiguous, conflicting-evidence,
  distractor-heavy. Frozen regression set + growing failure-mined set.
- **Retrieval** — Recall@k (the headline — a generator can't cite what wasn't retrieved), Precision@k,
  MRR, nDCG@k, context relevance, context coverage (the multi-hop killer). Report dense / BM25 / hybrid
  separately.
- **Generation** — faithfulness, answer relevance, hallucination rate (broken out on negatives),
  completeness. Run on gold context (pure) *and* real context (realistic); the delta blames
  retriever-vs-generator.
- **Citation** — citation precision/recall, grounding ratio, unsupported-claim rate, and
  **citation-span accuracy** (does the bbox localize the supporting text, or just the page — this is what
  makes it Perplexity-level).
- **Reranker** — same set with rerank on vs off; lift in nDCG@k, Recall@final_k, *and* downstream
  faithfulness, plus the latency cost.
- **Confidence calibration** — thresholds **derived from reliability curves**, not guessed; abstain
  threshold fixed against the negative subset via an ROC/PR curve; report ECE.
- **Pipeline** — experiment = (dataset_version, pipeline_config, model_versions) → run_id; per-question
  results stored (regressions hide in subsets); **CI-gated** — config changes rerun the frozen set and
  block merge on stat-sig drops with bootstrap CIs. Per-segment reporting always.
- **LLM-as-judge discipline** — validated against the human-audited subset, version-pinned, stronger
  model judges than generates, pairwise/reference-anchored over raw 1–10.

---

## 8. Data model (core tables)

All tables carry `created_at` + `updated_at` audit columns (only noteworthy extras are listed below).

```sql
users(id, email, hashed_pw, plan, created_at, updated_at)
  -- hashed_pw: argon2id. Dedupe and every query are scoped per user (multi-tenant).

documents(id, user_id, title, s3_key, size_bytes, page_count, status,
          doc_hash, mime, created_at, updated_at, deleted_at)
  -- status: UPLOADED|VALIDATING|PARSING|OCR|STRUCTURING|CHUNKING
  --         |EMBEDDING|INDEXING|READY|FAILED|DEGRADED
  -- status transitions are GUARDED (UPDATE ... WHERE status = expected) — Celery retries
  --   with acks_late can double-fire a stage; a stale transition must be a no-op.
  -- deleted_at: soft delete — the S3 objects and DB rows are reaped together by a cleanup job.
  -- UNIQUE (user_id, doc_hash) — SHA-256 dedupe is PER USER, never corpus-wide.

sections(id, document_id, parent_id, level, title, section_path,
         page_start)

chunks(id, document_id, parent_chunk_id, content, chunk_type,
       embedding vector(N), embed_model, embed_version,
       page_start, page_end,
       bboxes jsonb,        -- LIST of {page,[x0,y0,x1,y1]} normalized
       char_start, char_end, section_path, section_title,
       block_ids jsonb, token_count, min_ocr_confidence)
  -- HNSW on embedding; GIN tsvector on content ('english' + 'simple' configs)
  -- PARTITION BY HASH (document_id), 16 partitions — Postgres cannot partition per-value;
  --   the partition count is pinned at creation and is effectively immutable.
  -- Partitioning forces composite keys: PK (id, document_id); the parent-child self-FK is
  --   (parent_chunk_id, document_id) → (id, document_id) — parent and child always share a document.
  -- vector(N) pins the embedder dimension at migration time; switching to a different-dimension
  --   model is a schema migration + full re-embed (embed_model/embed_version make it selective).
  -- Doc-scoped ANN is a FILTERED HNSW scan within one hash partition — requires pgvector >= 0.8
  --   (iterative index scans) to avoid the classic filtered-HNSW recall hole.

ingestion_jobs(id, document_id, stage, state, error,
               checkpoint jsonb, retry_count, last_heartbeat)
  -- UNIQUE (document_id, stage) — a retried stage updates its row, never duplicates it.
  -- Every stage is idempotent-by-checkpoint: re-running from the persisted checkpoint is safe.

chats(id, user_id, document_id, title, created_at)
messages(id, chat_id, role, content, citations jsonb,
         token_usage, created_at)
  -- INDEX (chat_id, created_at) — the message-history read path.

extractions(id, document_id, type, content jsonb, confidence)

-- evaluation
eval_datasets(id, version, hash, created_at)
eval_runs(id, dataset_version, config jsonb, model_versions jsonb,
          metrics jsonb, created_at)
eval_results(id, run_id, question_id, raw jsonb, scores jsonb)

-- comparison
comparisons(id, doc_a_id, doc_b_id, status, summary jsonb,
            high_risk_count, config jsonb, created_at)
section_alignments(id, comparison_id, section_a_id, section_b_id,
                   relation, similarity, position_changed)
clause_changes(id, comparison_id, section_alignment_id,
               chunk_a_id, chunk_b_id, change_kind, change_type,
               old_value, new_value, direction, affected_party,
               summary, citations_a jsonb, citations_b jsonb,
               severity, risk_rationale, contradiction_of, confidence)
```

---

## 9. API surface

```
POST   /documents                      → 202 {job_id}      (presigned upload complete)
GET    /documents/{id}                 → status + metadata
GET    /documents/{id}/stream          → SSE ingestion progress
POST   /documents/{id}/chat            → SSE answer + per-claim citations
GET    /chats/{id}/messages
POST   /documents/{id}/summary         ?scope=full|section
POST   /documents/{id}/extract         clauses|deadlines|risks|obligations
POST   /search                         → semantic search across corpus
POST   /comparisons    {doc_a,doc_b}   → 202 {comparison_id}
GET    /comparisons/{id}               → status + counters (SSE)
GET    /comparisons/{id}/result        ?severity=high&type=liability&kind=changed
GET    /comparisons/{id}/summary       → severity-ranked exec summary
```

Ingestion/extraction/comparison are async (`202 + id`); chat streams; comparison results filter
server-side (a big diff has hundreds of changes).

---

## 10. Security

Multi-tenant isolation: **Phase 1 enforces app-layer scoping** — every service query filters by the
authenticated `user_id` (JWT auth, argon2id password hashing); **Postgres RLS is a flagged Phase 2
hardening task** (defense-in-depth on top of, not instead of, app-layer scoping). Presigned
time-limited S3 URLs with tenant-prefixed keys; file validation by magic bytes + size limits at the
server-side upload-finalize step (**malware scan: explicitly deferred** — documented gap, ClamAV slots
in at finalize when added); untrusted-PDF parser hardening (Celery time + memory limits, file-size and
page-count ceilings before parse — a decompression bomb must kill one task, not a worker fleet);
per-user SHA-256 dedupe (corpus-wide dedupe is a cross-tenant existence side channel);
**prompt-injection defense** (documents are untrusted — a contract can contain "ignore previous
instructions"; document content is sandboxed from the system prompt); encryption at rest; optional PII
detection/redaction; rate limiting + request body-size caps + CORS allowlist; audit logging;
SOC2-readiness posture for contract/financial data.

---

## 11. Build roadmap (dependency-ordered)

The dependency spine — **provenance schema → ingestion → retrieval → citation → eval → comparison** —
means each phase stacks additively with no rewrites.

### Phase 1 — MVP (~3–4 wks)
Upload large PDF → ask questions → answers that cite a page. Hybrid retrieval + RRF, parent-child chunks
with **full provenance**, page-level citations, chat history. *Mock:* OCR, rerank, entailment (structural
validation only). **Demoable:** 100-page upload, question, answer citing "p.14," click → viewer jumps
there.

### Phase 2 — Resume-worthy (~4–6 wks) ← peak ROI
Reranking, parent expansion, **entailment-verified citations**, **bbox-precise zoom-safe highlights**,
confidence + abstain, OCR fallback, and the **RAG evaluation harness**. **Interview line:** "citations are
validated by an entailment model, not the generator's say-so, and I measure faithfulness, citation
precision/recall, and Recall@k on a reverse-validated golden set with CI regression gating." *If you build
only through Phase 2, the project is already strong.*

### Phase 3 — Standout (~3–5 wks, leveraged)
**Semantic comparison engine** + comparison eval, clause/deadline/risk extraction, quality dashboard,
optional agentic multi-doc. Reuses ingestion embeddings, the entailment verifier, and the citation overlay
renderer wholesale — low marginal cost, high impact. **Demoable:** contract + amendment → "Liability cap
raised $1M→$5M [High]" → click → both docs scroll and highlight aligned clauses.

```
provenance schema ─┬─> ingestion ─> retrieval ─> rerank ─> citation+highlight
                   │                    │           │           │
                   │                    └───────────┴─> entailment verifier ─┬─> citation validation
                   │                                                         └─> comparison diff
 entailment verifier + eval metrics ─> RAG eval harness ─> comparison eval
 ingestion embeddings ───────────────────────────────────> comparison alignment
 PDF.js viewer (P1) ─> overlay highlights (P2) ─> comparison highlights (P3, reused)
```

---

## 12. Known limitations & what I'd revisit

*(The section that signals seniority — name the tradeoffs before an interviewer does.)*

- **pgvector ceiling** — chosen for transactional consistency; would migrate hot collections to
  Qdrant/Weaviate past ~10M vectors or if advanced metadata filtering at scale becomes the bottleneck.
- **LLM-as-judge is an instrument with error** — every eval number inherits judge variance; mitigated by
  human-audit calibration and version pinning, but it's a measured approximation, not ground truth.
- **OCR-degraded hierarchy** — scanned pages lack font metadata, so section detection falls back to
  numbering-only and is marked lower-confidence; a layout-detection model would improve this.
- **Comparison risk scoring is triage, not legal advice** — deliberately framed as "flagged for review";
  productionizing for real legal use would need domain-expert label sets and far more conservative
  thresholds.
- **Cost** — entailment verification and bidirectional-entailment diffs add LLM calls; mitigated by cosine
  pre-gating (only the ambiguous band hits an LLM) and semantic caching, but cost-per-document is a real
  scaling axis to monitor.

---

## 13. One-paragraph summary (for the résumé)

Lexa is a document-intelligence platform for 500–1000 page PDFs featuring a durable, page-parallel
ingestion pipeline built on an immutable IR (re-chunk without re-parse); hybrid retrieval (BM25 + dense,
RRF-fused) with cross-encoder reranking; **entailment-verified, bounding-box-precise citations** where the
model selects from a closed ID set and every citation is validated to actually support its claim; a
**semantic comparison engine** using cascading section→clause alignment and bidirectional entailment to
detect meaning-level changes while suppressing reworded-but-equivalent false positives; and a
**versioned, CI-gated RAG evaluation harness** measuring retrieval (Recall@k, nDCG), generation
(faithfulness, hallucination rate), and citation (precision/recall, span accuracy) against a
reverse-validated golden set with data-derived confidence calibration.
