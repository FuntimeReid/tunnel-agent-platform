"""Thin client for the RAGFlow HTTP API, authenticated as the internal account."""
from pathlib import Path

import httpx

from app.config import settings

DONE, FAILED, CANCELLED = "DONE", "FAIL", "CANCEL"
_RUN_CODES = {"3": DONE, "4": FAILED, "2": CANCELLED}


class RagflowError(Exception):
    def __init__(self, message: str, transient: bool):
        super().__init__(message)
        self.transient = transient


def _client(timeout=60) -> httpx.Client:
    return httpx.Client(base_url=settings()["ragflow"]["base_url"], timeout=timeout, trust_env=False,
                        headers={"Authorization": "Bearer " + settings().ragflow_api_key})


def _call(method, path, timeout=60, **kw):
    try:
        with _client(timeout) as c:
            r = c.request(method, path, **kw)
    except httpx.TransportError as e:
        raise RagflowError(f"RAGFlow 连接失败：{type(e).__name__}", transient=True)
    if r.status_code >= 500:
        raise RagflowError(f"RAGFlow 返回 HTTP {r.status_code}", transient=True)
    try:
        body = r.json()
    except ValueError:
        raise RagflowError(f"RAGFlow 返回了非 JSON 内容（HTTP {r.status_code}）", transient=True)
    if body.get("code") != 0:
        raise RagflowError(str(body.get("message") or body.get("code")), transient=False)
    return body.get("data")


def health():
    _call("GET", "/datasets", params={"page": 1, "page_size": 1}, timeout=5)


def find_dataset(name: str) -> dict | None:
    # Filtering by an unknown name returns a permission error rather than an empty
    # list, so page through the account's datasets and match exactly.
    page = 1
    while True:
        batch = _call("GET", "/datasets", params={"page": page, "page_size": 100}) or []
        for ds in batch:
            if ds.get("name") == name:
                return ds
        if len(batch) < 100:
            return None
        page += 1


def create_dataset(name: str, description: str, parser_config: dict) -> dict:
    return _call("POST", "/datasets", json={"name": name, "description": description,
                                            "chunk_method": settings()["library_defaults"]["chunk_method"],
                                            "parser_config": parser_config})


def upload(dataset_id: str, path: Path, filename: str) -> dict:
    with path.open("rb") as f:
        return _call("POST", f"/datasets/{dataset_id}/documents", timeout=600, files={"file": (filename, f)})[0]


def set_template(dataset_id: str, doc_id: str, chunk_method: str, parser_config: dict):
    # Always send parser_config: without it RAGFlow merges the template defaults,
    # and the naive defaults would switch layout_recognize back to DeepDOC.
    _call("PATCH", f"/datasets/{dataset_id}/documents/{doc_id}", json={"chunk_method": chunk_method, "parser_config": parser_config})


def parse(dataset_id: str, doc_id: str):
    _call("POST", f"/datasets/{dataset_id}/documents/parse", json={"document_ids": [doc_id]})


def document(dataset_id: str, doc_id: str) -> dict | None:
    docs = (_call("GET", f"/datasets/{dataset_id}/documents", params={"id": doc_id}) or {}).get("docs") or []
    if not docs:
        return None
    d = docs[0]
    d["run"] = _RUN_CODES.get(str(d.get("run")), str(d.get("run")))
    return d
