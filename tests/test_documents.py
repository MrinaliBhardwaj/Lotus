"""Task 3 gate tests: presigned upload → finalize → queued job (§2.1 #2/#3)."""

import uuid

from httpx import AsyncClient

from app.core.config import Settings
from app.models import JobStage
from tests.pdf_utils import make_pdf


async def register(client: AsyncClient) -> dict[str, str]:
    response = await client.post(
        "/auth/register",
        json={"email": f"{uuid.uuid4().hex}@example.com", "password": "correct-horse-battery"},
    )
    assert response.status_code == 201
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


async def create_and_upload(
    client: AsyncClient, headers: dict[str, str], data: bytes, title: str = "Contract"
) -> str:
    created = await client.post("/documents", json={"title": title}, headers=headers)
    assert created.status_code == 201
    body = created.json()
    put = await client.put(body["upload_url"], content=data, headers=headers)
    assert put.status_code == 200, put.text
    return str(body["id"])


async def test_upload_finalize_happy_path(
    db_client: AsyncClient, captured_stages: list[tuple[JobStage, uuid.UUID]]
) -> None:
    headers = await register(db_client)
    document_id = await create_and_upload(db_client, headers, make_pdf(pages=3))

    completed = await db_client.post(f"/documents/{document_id}/complete", headers=headers)
    assert completed.status_code == 202, completed.text
    body = completed.json()
    assert body["status"] == "VALIDATING"
    assert uuid.UUID(body["job_id"])

    detail = await db_client.get(f"/documents/{document_id}", headers=headers)
    assert detail.json()["status"] == "VALIDATING"
    assert detail.json()["size_bytes"] == len(make_pdf(pages=3))
    assert captured_stages == [(JobStage.VALIDATE, uuid.UUID(document_id))]


async def test_finalize_rejects_non_pdf(
    db_client: AsyncClient, captured_stages: list[tuple[JobStage, uuid.UUID]]
) -> None:
    headers = await register(db_client)
    document_id = await create_and_upload(db_client, headers, b"MZ this is not a pdf" * 10)

    rejected = await db_client.post(f"/documents/{document_id}/complete", headers=headers)
    assert rejected.status_code == 422
    assert "not a PDF" in rejected.json()["detail"]
    assert captured_stages == []
    # the offending object was deleted server-side
    again = await db_client.post(f"/documents/{document_id}/complete", headers=headers)
    assert "no uploaded file" in again.json()["detail"]


async def test_finalize_rejects_oversize_and_deletes_object(
    db_client: AsyncClient, db_settings: Settings
) -> None:
    headers = await register(db_client)
    pdf = make_pdf(pages=2)
    document_id = await create_and_upload(db_client, headers, pdf)

    db_settings.max_upload_bytes = len(pdf) - 1  # shrink the cap after the upload landed
    rejected = await db_client.post(f"/documents/{document_id}/complete", headers=headers)
    assert rejected.status_code == 422
    assert "limit" in rejected.json()["detail"]

    db_settings.max_upload_bytes = 500 * 1024 * 1024
    gone = await db_client.post(f"/documents/{document_id}/complete", headers=headers)
    assert "no uploaded file" in gone.json()["detail"]


async def test_per_user_dedupe_conflict(
    db_client: AsyncClient, captured_stages: list[tuple[JobStage, uuid.UUID]]
) -> None:
    headers = await register(db_client)
    pdf = make_pdf(pages=2, text="identical bytes both times")

    first = await create_and_upload(db_client, headers, pdf)
    assert (
        await db_client.post(f"/documents/{first}/complete", headers=headers)
    ).status_code == 202

    second = await create_and_upload(db_client, headers, pdf, title="Same file again")
    duplicate = await db_client.post(f"/documents/{second}/complete", headers=headers)
    assert duplicate.status_code == 409
    # the duplicate's row was removed
    assert (await db_client.get(f"/documents/{second}", headers=headers)).status_code == 404

    # a DIFFERENT user uploading the same bytes is fine (§2.1 #3 — no side channel)
    other_headers = await register(db_client)
    other_doc = await create_and_upload(db_client, other_headers, pdf)
    ok = await db_client.post(f"/documents/{other_doc}/complete", headers=other_headers)
    assert ok.status_code == 202


async def test_finalize_twice_conflicts(
    db_client: AsyncClient, captured_stages: list[tuple[JobStage, uuid.UUID]]
) -> None:
    headers = await register(db_client)
    document_id = await create_and_upload(db_client, headers, make_pdf(pages=1))
    assert (
        await db_client.post(f"/documents/{document_id}/complete", headers=headers)
    ).status_code == 202
    again = await db_client.post(f"/documents/{document_id}/complete", headers=headers)
    assert again.status_code == 409


async def test_tenant_isolation_on_documents(db_client: AsyncClient) -> None:
    headers_a = await register(db_client)
    headers_b = await register(db_client)
    document_id = await create_and_upload(db_client, headers_a, make_pdf(pages=1))

    # user B can neither see nor finalize A's document — 404, not 403 (invariant 7)
    assert (
        await db_client.get(f"/documents/{document_id}", headers=headers_b)
    ).status_code == 404
    assert (
        await db_client.post(f"/documents/{document_id}/complete", headers=headers_b)
    ).status_code == 404
    assert (await db_client.get("/documents", headers=headers_b)).json() == []


async def test_local_upload_rejects_foreign_key_prefix(db_client: AsyncClient) -> None:
    headers_a = await register(db_client)
    headers_b = await register(db_client)
    created = await db_client.post("/documents", json={"title": "A"}, headers=headers_a)
    upload_url = created.json()["upload_url"]
    # B tries to PUT into A's tenant prefix
    forged = await db_client.put(upload_url, content=b"%PDF-fake", headers=headers_b)
    assert forged.status_code == 403


async def test_download_url_roundtrip(db_client: AsyncClient) -> None:
    headers = await register(db_client)
    pdf = make_pdf(pages=1)
    document_id = await create_and_upload(db_client, headers, pdf)

    response = await db_client.get(f"/documents/{document_id}/download-url", headers=headers)
    assert response.status_code == 200
    fetched = await db_client.get(response.json()["url"], headers=headers)
    assert fetched.status_code == 200
    assert fetched.content == pdf

    # another tenant can neither mint the URL nor fetch the object
    headers_b = await register(db_client)
    assert (
        await db_client.get(f"/documents/{document_id}/download-url", headers=headers_b)
    ).status_code == 404
    assert (await db_client.get(response.json()["url"], headers=headers_b)).status_code == 403


async def test_delete_document_soft_deletes(db_client: AsyncClient) -> None:
    headers = await register(db_client)
    document_id = await create_and_upload(db_client, headers, make_pdf(pages=1))
    assert (
        await db_client.delete(f"/documents/{document_id}", headers=headers)
    ).status_code == 204
    assert (await db_client.get(f"/documents/{document_id}", headers=headers)).status_code == 404
    assert (await db_client.get("/documents", headers=headers)).json() == []
