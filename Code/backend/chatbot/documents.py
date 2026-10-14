"""Where uploaded documents go: the Blob Storage container the search indexer reads, or a local folder."""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .config import ALLOWED_EXTENSIONS, Settings

_UNSAFE = re.compile(r"[^A-Za-z0-9._ ()-]+")


@dataclass(frozen=True)
class StoredDocument:
    name: str
    size: int
    last_modified: str | None = None


class DocumentStore(Protocol):
    def save(self, name: str, data: bytes, content_type: str | None) -> StoredDocument: ...
    def list(self) -> list[StoredDocument]: ...
    def delete(self, name: str) -> bool: ...


def safe_name(filename: str) -> str:
    """A blob name without folders or unusual characters. Raises ValueError for unsupported types."""
    name = Path(filename.replace("\\", "/")).name.strip()
    name = _UNSAFE.sub("-", name).strip(" .-")
    stem, dot, ext = name.rpartition(".")
    stem = stem.strip(" .-")
    if not dot or not stem:
        raise ValueError("The file needs a name and an extension.")
    if f".{ext.lower()}" not in ALLOWED_EXTENSIONS:
        raise ValueError(f"Unsupported file type .{ext}. Allowed: {', '.join(ALLOWED_EXTENSIONS)}")
    return f"{stem[:150]}.{ext.lower()}"


class BlobDocumentStore:
    def __init__(self, settings: Settings):
        from azure.storage.blob import BlobServiceClient

        from .azure_auth import get_credential

        service = BlobServiceClient(account_url=settings.storage_account_url, credential=get_credential())
        self.container = service.get_container_client(settings.storage_container)

    def save(self, name: str, data: bytes, content_type: str | None) -> StoredDocument:
        from azure.storage.blob import ContentSettings

        self.container.upload_blob(name, data, overwrite=True,
                                   content_settings=ContentSettings(content_type=content_type or "application/octet-stream"))
        return StoredDocument(name=name, size=len(data))

    def list(self) -> list[StoredDocument]:
        return [StoredDocument(b.name, b.size, b.last_modified.isoformat() if b.last_modified else None)
                for b in self.container.list_blobs()]

    def delete(self, name: str) -> bool:
        from azure.core.exceptions import ResourceNotFoundError

        try:
            self.container.delete_blob(name)
            return True
        except ResourceNotFoundError:
            return False


class LocalDocumentStore:
    def __init__(self, folder: Path):
        self.folder = folder

    def save(self, name: str, data: bytes, content_type: str | None) -> StoredDocument:
        self.folder.mkdir(parents=True, exist_ok=True)
        (self.folder / name).write_bytes(data)
        return StoredDocument(name=name, size=len(data))

    def list(self) -> list[StoredDocument]:
        if not self.folder.is_dir():
            return []
        return [StoredDocument(p.name, p.stat().st_size) for p in sorted(self.folder.iterdir()) if p.is_file()]

    def delete(self, name: str) -> bool:
        path = self.folder / name
        if path.is_file():
            path.unlink()
            return True
        return False
