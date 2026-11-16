"""Private object storage with an explicit local fallback when no bucket exists."""
from __future__ import annotations

import logging
from pathlib import Path, PurePosixPath

from app.core.config import get_settings

logger = logging.getLogger("nyaya.storage")
settings = get_settings()


class StorageService:
    def __init__(self):
        self._client = None
        self._root = Path("artifacts/api-server/.local-data/storage").resolve()
        self._root.mkdir(parents=True, exist_ok=True)
        try:
            from replit.object_storage import Client

            bucket = settings.default_object_storage_bucket_id
            self._client = Client(bucket_id=bucket or None)
        except Exception as exc:
            if settings.is_production:
                logger.error("object_storage_unavailable_in_production", extra={"event": "storage_init"})
            else:
                logger.warning("object_storage_not_attached_using_local_fallback", extra={"event": "storage_fallback"})

    @property
    def kind(self) -> str:
        return "replit-app-storage" if self._client is not None else "local-fallback"

    @staticmethod
    def _safe_name(name: str) -> str:
        path = PurePosixPath(name)
        if path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in path.parts):
            raise ValueError("Invalid storage object name.")
        return path.as_posix()

    def upload(self, name: str, data: bytes) -> None:
        safe_name = self._safe_name(name)
        if self._client is not None:
            self._client.upload_from_bytes(safe_name, data)
            return
        target = (self._root / safe_name).resolve()
        if self._root not in target.parents:
            raise ValueError("Invalid storage object name.")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)

    def download(self, name: str) -> bytes:
        safe_name = self._safe_name(name)
        if self._client is not None:
            return self._client.download_as_bytes(safe_name)
        target = (self._root / safe_name).resolve()
        if self._root not in target.parents:
            raise ValueError("Invalid storage object name.")
        return target.read_bytes()

    def delete(self, name: str) -> None:
        safe_name = self._safe_name(name)
        if self._client is not None:
            self._client.delete(safe_name, ignore_not_found=True)
            return
        target = (self._root / safe_name).resolve()
        if self._root not in target.parents:
            raise ValueError("Invalid storage object name.")
        target.unlink(missing_ok=True)

    def list_objects(self, max_results: int = 1, prefix: str | None = None) -> list:
        if self._client is not None:
            return self._client.list(max_results=max_results, prefix=prefix)
        objects = []
        for path in self._root.rglob("*"):
            if path.is_file():
                key = path.relative_to(self._root).as_posix()
                if prefix is None or key.startswith(prefix):
                    objects.append(key)
                    if len(objects) >= max_results:
                        break
        return objects


storage = StorageService()
