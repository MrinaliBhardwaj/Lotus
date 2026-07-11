"""Validate/triage stage (sync — runs in Celery workers, §2.1 #7).

Opens the untrusted PDF (the worker's §2.1 #8 memory/time limits are the blast
shield), checks encryption and the page ceiling, probes every page's text
layer, and stores a page manifest classifying each page ``text|scanned|mixed``.
Phase 1 has no OCR: any scanned/mixed page fails the document with a clear
message (CLAUDE.md Task 3 mock).
"""

import asyncio
import json
import logging
import uuid
from typing import Any

import fitz  # PyMuPDF

from app.core.config import Settings
from app.models import Document, DocumentStatus, JobStage, JobState
from app.services.ingestion import pipeline, status
from app.storage.base import ObjectStorage, artifact_key
from app.workers.session import worker_session

logger = logging.getLogger(__name__)

MANIFEST_ARTIFACT = "page_manifest.json"
# below this many characters a page's text layer is considered vestigial
_TEXT_CHAR_THRESHOLD = 32


def classify_page(page: fitz.Page) -> tuple[str, int]:
    """Return (mode, char_count) for one page. Heuristic, documented:
    a real text layer → text; images with no text → scanned; images with a
    vestigial text layer (a few OCR crumbs or a caption) → mixed."""
    chars = len(page.get_text("text").strip())
    has_images = bool(page.get_images(full=False))
    if chars >= _TEXT_CHAR_THRESHOLD:
        return "text", chars
    if has_images:
        return ("mixed", chars) if chars > 0 else ("scanned", chars)
    return "text", chars  # blank or sparse vector page — nothing to OCR


def run_validate(document_id: uuid.UUID, *, settings: Settings, storage: ObjectStorage) -> None:
    with worker_session() as session:
        document = session.get(Document, document_id)
        if document is None or document.status is not DocumentStatus.VALIDATING:
            logger.info("validate(%s): stale or missing — no-op", document_id)
            return
        job = status.start_job_sync(session, document_id, JobStage.VALIDATE)

        data = asyncio.run(storage.get(document.s3_key))
        try:
            pdf = fitz.open(stream=data, filetype="pdf")
        except Exception:
            status.fail_document_sync(
                session, document_id, job, "file could not be opened as a PDF"
            )
            return
        with pdf:
            if pdf.needs_pass or pdf.is_encrypted:
                status.fail_document_sync(
                    session, document_id, job, "encrypted PDFs are not supported"
                )
                return
            if pdf.page_count > settings.max_pdf_pages:
                status.fail_document_sync(
                    session,
                    document_id,
                    job,
                    f"document has {pdf.page_count} pages; the limit is {settings.max_pdf_pages}",
                )
                return

            pages: list[dict[str, Any]] = []
            counts = {"text": 0, "scanned": 0, "mixed": 0}
            for index, page in enumerate(pdf):
                mode, chars = classify_page(page)
                counts[mode] += 1
                pages.append({"page": index + 1, "mode": mode, "chars": chars})
            page_count = pdf.page_count

        manifest_key = artifact_key(document.user_id, document.id, MANIFEST_ARTIFACT)
        manifest = {"pages": pages, "counts": counts}
        asyncio.run(
            storage.put(
                manifest_key,
                json.dumps(manifest).encode(),
                content_type="application/json",
            )
        )

        needs_ocr = counts["scanned"] + counts["mixed"]
        if needs_ocr:
            status.fail_document_sync(
                session,
                document_id,
                job,
                f"{needs_ocr} of {page_count} pages have no usable text layer and would "
                "need OCR, which is not supported yet",
            )
            return

        document.page_count = page_count
        session.commit()
        status.finish_job_sync(
            session,
            job,
            JobState.SUCCEEDED,
            checkpoint={"manifest_key": manifest_key, "page_count": page_count, "counts": counts},
        )
        if status.transition_document_sync(
            session, document_id, DocumentStatus.VALIDATING, DocumentStatus.PARSING
        ):
            session.commit()
            pipeline.enqueue_stage(JobStage.PARSE, document_id)
