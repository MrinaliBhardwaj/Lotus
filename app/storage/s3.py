"""S3-compatible adapter (AWS S3, Cloudflare R2, or MinIO via endpoint_url)."""

from collections.abc import AsyncIterator
from typing import Any

import aioboto3
from botocore.config import Config as BotoConfig
from botocore.exceptions import ClientError

from app.core.config import Settings
from app.core.exceptions import StorageError
from app.storage.base import STREAM_CHUNK_BYTES, ObjectInfo, ObjectStorage, PresignedUpload


class S3Storage(ObjectStorage):
    def __init__(self, settings: Settings) -> None:
        self._bucket = settings.s3_bucket
        self._session = aioboto3.Session()
        self._client_kwargs: dict[str, Any] = {
            "endpoint_url": settings.s3_endpoint_url,
            "aws_access_key_id": settings.s3_access_key_id,
            "aws_secret_access_key": settings.s3_secret_access_key,
            "region_name": settings.s3_region,
            "config": BotoConfig(signature_version="s3v4"),
        }

    def _client(self) -> Any:
        return self._session.client("s3", **self._client_kwargs)

    async def presign_upload(
        self, key: str, *, content_type: str, max_bytes: int, expires_in: int
    ) -> PresignedUpload:
        # presigned POST (not PUT) so the policy enforces the size ceiling
        # server-side — a bare presigned PUT can't cap upload size (H2)
        async with self._client() as s3:
            post = await s3.generate_presigned_post(
                Bucket=self._bucket,
                Key=key,
                Fields={"Content-Type": content_type},
                Conditions=[
                    {"Content-Type": content_type},
                    ["content-length-range", 0, max_bytes],
                ],
                ExpiresIn=expires_in,
            )
        return PresignedUpload(url=post["url"], method="POST", fields=post["fields"])

    async def presign_download(self, key: str, *, expires_in: int) -> str:
        async with self._client() as s3:
            url: str = await s3.generate_presigned_url(
                "get_object",
                Params={"Bucket": self._bucket, "Key": key},
                ExpiresIn=expires_in,
            )
            return url

    async def put(self, key: str, data: bytes, *, content_type: str) -> None:
        async with self._client() as s3:
            try:
                await s3.put_object(
                    Bucket=self._bucket, Key=key, Body=data, ContentType=content_type
                )
            except ClientError as exc:
                raise StorageError(f"put failed for {key}") from exc

    async def get(self, key: str) -> bytes:
        async with self._client() as s3:
            try:
                response = await s3.get_object(Bucket=self._bucket, Key=key)
                body: bytes = await response["Body"].read()
                return body
            except ClientError as exc:
                raise StorageError(f"get failed for {key}") from exc

    async def get_range(self, key: str, start: int, end: int) -> bytes:
        async with self._client() as s3:
            try:
                response = await s3.get_object(
                    Bucket=self._bucket, Key=key, Range=f"bytes={start}-{end}"
                )
                body: bytes = await response["Body"].read()
                return body
            except ClientError as exc:
                raise StorageError(f"range get failed for {key}") from exc

    async def stream(
        self, key: str, *, chunk_size: int = STREAM_CHUNK_BYTES
    ) -> AsyncIterator[bytes]:
        async with self._client() as s3:
            try:
                response = await s3.get_object(Bucket=self._bucket, Key=key)
                async with response["Body"] as body:
                    while chunk := await body.read(chunk_size):
                        yield chunk
            except ClientError as exc:
                raise StorageError(f"stream failed for {key}") from exc

    async def head(self, key: str) -> ObjectInfo | None:
        async with self._client() as s3:
            try:
                response = await s3.head_object(Bucket=self._bucket, Key=key)
            except ClientError as exc:
                if exc.response.get("Error", {}).get("Code") in ("404", "NoSuchKey", "NotFound"):
                    return None
                raise StorageError(f"head failed for {key}") from exc
            return ObjectInfo(
                key=key,
                size_bytes=response["ContentLength"],
                content_type=response.get("ContentType"),
            )

    async def delete(self, key: str) -> None:
        async with self._client() as s3:
            try:
                await s3.delete_object(Bucket=self._bucket, Key=key)
            except ClientError as exc:
                raise StorageError(f"delete failed for {key}") from exc
