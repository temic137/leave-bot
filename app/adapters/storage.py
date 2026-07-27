from pathlib import Path
from uuid import uuid4

import httpx

from app.core.config import settings


ALLOWED_DOCUMENT_TYPES = {
    "application/pdf": b"%PDF-",
    "image/jpeg": b"\xff\xd8\xff",
    "image/png": b"\x89PNG\r\n\x1a\n",
}


class DocumentStorage:
    def store_bytes(self, filename: str, content: bytes, content_type: str = "application/octet-stream") -> str:
        raise NotImplementedError

    def get_url(self, document_key: str) -> str:
        raise NotImplementedError


class LocalDocumentStorage(DocumentStorage):
    def __init__(self, root: str = "local_documents"):
        self.root = Path(root)
        self.root.mkdir(exist_ok=True)

    def store_bytes(self, filename: str, content: bytes, content_type: str = "application/octet-stream") -> str:
        key = f"{uuid4()}-{filename}"
        path = self.root / key
        path.write_bytes(content)
        return key

    def get_url(self, document_key: str) -> str:
        return str((self.root / document_key).resolve())


class AutochekDocumentStorage(DocumentStorage):
    def store_bytes(self, filename: str, content: bytes, content_type: str) -> str:
        if not settings.autochek_upload_url:
            raise RuntimeError("AUTOCHEK_UPLOAD_URL is not configured")
        validate_document(content, content_type)
        headers = {
            "Accept": "application/json",
            "x-autochek-app": "marketplace_web",
            "x-alt-app": settings.autochek_alt_app,
        }
        if settings.autochek_api_token:
            token = settings.autochek_api_token
            headers["Authorization"] = token if token.lower().startswith("bearer ") else f"Bearer {token}"
        if settings.autochek_api_key:
            headers["x-api-key"] = settings.autochek_api_key

        response = httpx.post(
            settings.autochek_upload_url,
            headers=headers,
            files={"file": (Path(filename).name, content, content_type)},
            timeout=180,
        )
        response.raise_for_status()
        payload = response.json()
        candidates = [payload, payload.get("data"), payload.get("document"), payload.get("file")]
        for candidate in candidates:
            if isinstance(candidate, str) and candidate.startswith("https://"):
                return candidate
            if isinstance(candidate, dict):
                for key in ("url", "fileUrl", "file_url", "documentUrl", "location", "signedUrl"):
                    value = candidate.get(key)
                    if isinstance(value, str) and value.startswith("https://"):
                        return value
        raise RuntimeError("Autochek upload response did not contain an HTTPS file URL")

    def get_url(self, document_key: str) -> str:
        return document_key


def validate_document(content: bytes, content_type: str) -> None:
    signature = ALLOWED_DOCUMENT_TYPES.get(content_type)
    if signature is None:
        raise ValueError("Only PDF, JPEG, and PNG documents are allowed.")
    if not content.startswith(signature):
        raise ValueError("The document contents do not match its reported file type.")
    if len(content) > settings.document_max_bytes:
        raise ValueError(
            f"The document is too large. The current upload limit is "
            f"{settings.document_max_bytes // 1000} KB."
        )
