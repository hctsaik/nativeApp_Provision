"""MinIO / S3 ObjectStore adapter (Slice 3).

Immutable objects with client-side SHA-256. Critical detail (03_DOMAIN_SPEC.md
§5): a MinIO multipart ETag is NOT a SHA-256, so ``put`` streams the source to a
spooled temp file, computes SHA-256 + size itself, then uploads — the digest we
return and store in the registry is always the true content hash.

Driver (``minio``) is imported lazily. Unverified on the WDAC build box (no local
MinIO); the shared object-store contract tests exercise it in CI when
PROVISION_MINIO_ENDPOINT is set.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
from typing import BinaryIO, Iterable

from provision_builder.package_errors import ObjectStoreUnavailable

_CHUNK = 1024 * 1024


class MinioObjectStore:
    def __init__(
        self,
        endpoint: str,
        access_key: str,
        secret_key: str,
        bucket: str,
        *,
        secure: bool = True,
    ):
        try:
            from minio import Minio
        except ImportError as exc:  # pragma: no cover - optional driver
            raise ObjectStoreUnavailable("minio package is required for MinioObjectStore") from exc
        self._client = Minio(endpoint, access_key=access_key, secret_key=secret_key, secure=secure)
        self.bucket = bucket
        try:
            if not self._client.bucket_exists(bucket):
                self._client.make_bucket(bucket)
        except Exception as exc:  # pragma: no cover - network/driver
            raise ObjectStoreUnavailable(f"cannot reach MinIO bucket {bucket}: {exc}") from exc

    def exists(self, object_key: str) -> bool:
        from minio.error import S3Error

        try:
            self._client.stat_object(self.bucket, object_key)
            return True
        except S3Error as exc:
            if exc.code in {"NoSuchKey", "NoSuchObject"}:
                return False
            raise

    def put(self, object_key: str, source: BinaryIO) -> tuple[str, int]:
        from minio.error import S3Error

        if self.exists(object_key):
            raise FileExistsError(f"immutable object already exists: {object_key}")
        digester = hashlib.sha256()
        size = 0
        fd, temp_name = tempfile.mkstemp(prefix=".minio-upload-")
        try:
            with os.fdopen(fd, "wb") as spool:
                while chunk := source.read(_CHUNK):
                    spool.write(chunk)
                    digester.update(chunk)
                    size += len(chunk)
            with open(temp_name, "rb") as body:
                try:
                    self._client.put_object(self.bucket, object_key, body, length=size)
                except S3Error as exc:  # pragma: no cover - network/driver
                    raise ObjectStoreUnavailable(f"upload failed: {exc}") from exc
        finally:
            os.unlink(temp_name)
        return digester.hexdigest(), size

    def open(self, object_key: str) -> BinaryIO:
        from minio.error import S3Error

        fd, temp_name = tempfile.mkstemp(prefix=".minio-download-")
        try:
            response = None
            try:
                response = self._client.get_object(self.bucket, object_key)
                with os.fdopen(fd, "wb") as target:
                    for chunk in response.stream(_CHUNK):
                        target.write(chunk)
            except S3Error as exc:
                if exc.code in {"NoSuchKey", "NoSuchObject"}:
                    raise FileNotFoundError(object_key) from exc
                raise
            finally:
                if response is not None:
                    response.close()
                    response.release_conn()
        except BaseException:
            os.unlink(temp_name)
            raise
        handle = open(temp_name, "rb")
        if os.name != "nt":
            os.unlink(temp_name)  # POSIX: file stays readable via the open handle
        return handle

    def iter_keys(self) -> Iterable[str]:
        for obj in self._client.list_objects(self.bucket, recursive=True):
            yield obj.object_name

    def delete(self, object_key: str) -> None:
        self._client.remove_object(self.bucket, object_key)
