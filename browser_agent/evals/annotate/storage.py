"""Object storage for Trace files and blobs: Cloudflare R2 (or MinIO locally) via the S3 API.

Keys:
    blobs/sha256/<hex>                    content-addressed, immutable
    trials/<trial_id>/trial.json
    trials/<trial_id>/events.jsonl
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from .config import Settings

# Trace text is untrusted live-web content. Only these types are served as themselves;
# anything else (text/html, image/svg+xml, …) is served as an opaque download type.
SAFE_MEDIA_TYPES = {
    "text/plain",
    "application/json",
    "image/png",
    "image/jpeg",
    "image/webp",
    "image/gif",
}


def safe_media_type(media_type: str | None) -> str:
    base = (media_type or "").split(";")[0].strip().lower()
    if base in SAFE_MEDIA_TYPES:
        return base + ("; charset=utf-8" if base.startswith("text/") else "")
    return "application/octet-stream"


def blob_key(sha256: str) -> str:
    return f"blobs/sha256/{sha256}"


def trial_key(trial_id: str, name: str) -> str:
    return f"trials/{trial_id}/{name}"


class BlobStore:
    """Thin wrapper around a boto3 S3 client (tests pass an in-memory fake client)."""

    def __init__(self, client: Any, bucket: str) -> None:
        self.client = client
        self.bucket = bucket

    def put(self, key: str, data: bytes, content_type: str) -> None:
        self.client.put_object(
            Bucket=self.bucket, Key=key, Body=data, ContentType=safe_media_type(content_type)
        )

    def get_bytes(self, key: str) -> bytes:
        obj = self.client.get_object(Bucket=self.bucket, Key=key)
        return obj["Body"].read()

    def iter_bytes(self, key: str, chunk_size: int = 64 * 1024) -> Iterator[bytes]:
        obj = self.client.get_object(Bucket=self.bucket, Key=key)
        body = obj["Body"]
        try:
            yield from body.iter_chunks(chunk_size)
        finally:
            body.close()

    def presign(self, key: str, media_type: str, expires_s: int = 120) -> str:
        return self.client.generate_presigned_url(
            "get_object",
            Params={
                "Bucket": self.bucket,
                "Key": key,
                "ResponseContentType": safe_media_type(media_type),
            },
            ExpiresIn=expires_s,
        )


def make_s3_client(settings: Settings) -> Any:
    import boto3
    from botocore.config import Config

    return boto3.client(  # pyright: ignore[reportUnknownMemberType]
        "s3",
        endpoint_url=settings.s3_endpoint_url,
        aws_access_key_id=settings.s3_access_key_id,
        aws_secret_access_key=settings.s3_secret_access_key,
        region_name=settings.s3_region,
        config=Config(
            signature_version="s3v4",
            s3={"addressing_style": "path"},  # R2 and MinIO both accept path-style
            retries={"max_attempts": 4, "mode": "standard"},
            request_checksum_calculation="when_required",
            response_checksum_validation="when_required",
        ),
    )
