from pathlib import Path
from uuid import uuid4


class DocumentStorage:
    def store_bytes(self, filename: str, content: bytes) -> str:
        raise NotImplementedError

    def get_url(self, document_key: str) -> str:
        raise NotImplementedError


class LocalDocumentStorage(DocumentStorage):
    def __init__(self, root: str = "local_documents"):
        self.root = Path(root)
        self.root.mkdir(exist_ok=True)

    def store_bytes(self, filename: str, content: bytes) -> str:
        key = f"{uuid4()}-{filename}"
        path = self.root / key
        path.write_bytes(content)
        return key

    def get_url(self, document_key: str) -> str:
        return str((self.root / document_key).resolve())

