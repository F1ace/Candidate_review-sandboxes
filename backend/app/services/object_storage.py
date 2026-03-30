from __future__ import annotations

from io import BytesIO

from minio import Minio
from minio.error import S3Error

from ..config import settings


class ObjectStorage:
    def __init__(self) -> None:
        self.bucket = settings.minio_bucket
        self.client = Minio(
            settings.minio_endpoint,
            access_key=settings.minio_access_key,
            secret_key=settings.minio_secret_key,
            secure=bool(settings.minio_secure),
        )

    def ensure_bucket(self) -> None:
        try:
            if not self.client.bucket_exists(self.bucket):
                self.client.make_bucket(self.bucket)
        except S3Error as exc:
            raise RuntimeError(f"Unable to initialize bucket '{self.bucket}': {exc}") from exc

    def put_bytes(self, object_key: str, payload: bytes, content_type: str) -> dict[str, str | int]:
        self.ensure_bucket()
        data = BytesIO(payload)
        self.client.put_object(
            self.bucket,
            object_key,
            data,
            length=len(payload),
            content_type=content_type,
        )
        return {
            "bucket": self.bucket,
            "object_key": object_key,
            "size_bytes": len(payload),
        }


storage_service = ObjectStorage()
