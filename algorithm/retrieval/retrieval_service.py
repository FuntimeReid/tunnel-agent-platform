"""Single-library retrieval tool; reuse the selected evidence algorithm unchanged."""
import http.client
import json
import logging
import os
from pathlib import Path
import socket
import sys
import time
from typing import Literal
from urllib.parse import urlsplit
import uuid

from fastapi import FastAPI, Header
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.exceptions import HTTPException

INGEST_ROOT = Path(os.environ.get("INGEST_ROOT", Path(__file__).resolve().parents[1] / "ingest_gateway"))
API_BASE = os.environ.get("RETRIEVAL_API_BASE", "http://127.0.0.1:29390/api/v1").rstrip("/")
sys.path.insert(0, str(INGEST_ROOT))
log = logging.getLogger("retrieval")
logging.basicConfig(level=logging.INFO)
app = FastAPI(title="知识库检索", docs_url=None, redoc_url=None)


class SearchInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    library_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    text: str = Field(min_length=1, max_length=8000)
    mode: Literal["standard", "medium", "high"] = "standard"
    top_n: int = Field(default=12, ge=1, le=64)


def failure(status, code, message, request_id=None):
    return JSONResponse(status_code=status, content={"code": code, "message": message,
                        "request_id": request_id or uuid.uuid4().hex[:16]})


@app.exception_handler(RequestValidationError)
async def invalid_request(request, exc):
    return failure(400, "invalid_request", "参数不正确，请检查请求字段、类型和长度。")


@app.exception_handler(HTTPException)
async def http_error(request, exc):
    return failure(exc.status_code, "not_found" if exc.status_code == 404 else "invalid_request",
                   "接口不存在" if exc.status_code == 404 else "请求方法不正确")


def registry_library(library_id):
    from app import db
    return db.one("SELECT id FROM library WHERE id=%s", [library_id])


def algorithm_call(path, payload=None, timeout=930):
    from app.config import settings
    target = urlsplit(API_BASE)
    connection = http.client.HTTPConnection(target.hostname, target.port, timeout=timeout)
    try:
        connection.request("POST" if payload is not None else "GET", target.path + path,
            json.dumps(payload, ensure_ascii=False).encode() if payload is not None else None,
            {"Authorization": "Bearer " + settings().ragflow_api_key,
             "Content-Type": "application/json"})
        response = connection.getresponse()
        return response.status, json.loads(response.read())
    finally:
        connection.close()


@app.get("/retrieve/health")
def health():
    from app import db
    checks = {}
    try:
        checks["registry"] = "ok" if db.one("SELECT 1 AS ok") else "error"
    except Exception:
        checks["registry"] = "error"
    try:
        status, body = algorithm_call("/datasets?page=1&page_size=1", timeout=10)
        checks["algorithm"] = "ok" if status == 200 and body.get("code") == 0 else "error"
    except Exception:
        checks["algorithm"] = "error"
    ok = all(value == "ok" for value in checks.values())
    result = {
        "code": 0 if ok else "upstream_unavailable", "request_id": uuid.uuid4().hex[:16],
        "data": {"status": "ok" if ok else "degraded", "checks": checks}}
    if not ok:
        result["message"] = "检索服务部分依赖不可用。"
    return JSONResponse(status_code=200 if ok else 503, content=result)


@app.post("/retrieve/search")
def search(body: SearchInput, x_caller_id: str | None = Header(default=None)):
    request_id = uuid.uuid4().hex[:16]
    if not body.text.strip():
        return failure(400, "invalid_request", "text不能为空白文本。", request_id)
    try:
        allowed = registry_library(body.library_id)
    except Exception as exc:
        log.error("registry request_id=%s error_type=%s", request_id, type(exc).__name__)
        return failure(503, "upstream_unavailable", "暂时无法查询知识库登记表。", request_id)
    if not allowed:
        return failure(404, "library_not_found", "知识库不存在或不是通过入库接口创建的对外库。", request_id)
    started = time.monotonic()
    try:
        status, result = algorithm_call("/algorithm/evidence", {
            "question": body.text.strip(), "dataset_ids": [body.library_id],
            "mode": body.mode, "top_n": body.top_n, "output": "chunks",
            "timeout_seconds": 900, "diagnostics": False})
        if status != 200 or result.get("code") != 0:
            log.warning("algorithm request_id=%s upstream_request_id=%s status=%s",
                        request_id, result.get("request_id"), status)
            return failure(504 if status == 504 else 502, "upstream_error",
                           "算法服务暂时无法完成检索，请按request_id查询日志。", request_id)
        data = result["data"]
        chunks = [{"chunk_id": row["chunk_id"], "doc_id": row["document_id"],
                   "document_name": row["document_name"], "text": row["text"],
                   "pages": row.get("pages"), "positions": row.get("positions"),
                   "score": row.get("score")} for row in data.get("sources", [])]
        log.info(json.dumps({"request_id": request_id, "caller_id": (x_caller_id or "").strip()[:128] or "anonymous",
            "library_id": body.library_id, "mode": body.mode, "chunks": len(chunks),
            "algorithm_request_id": result.get("request_id"),
            "seconds": round(time.monotonic() - started, 3)}, ensure_ascii=False))
        return {"code": 0, "request_id": request_id, "data": {
            "chunks": chunks, "mode": data.get("mode", body.mode),
            "elapsed_seconds": data.get("elapsed_seconds"), "usage": data.get("usage"),
            "implementation_revision": data.get("implementation_revision"),
            "evidence_status": data.get("evidence_status"),
            "missing_requested_sources": data.get("missing_requested_sources", []),
            "partial": bool(data.get("research_partial"))}}
    except (TimeoutError, socket.timeout):
        return failure(504, "upstream_timeout", "检索超时；请先查询日志，不要立即重复提交。", request_id)
    except Exception as exc:
        log.error("algorithm request_id=%s error_type=%s", request_id, type(exc).__name__)
        return failure(502, "upstream_error", "算法服务暂时不可用。", request_id)


from library_catalog import install_catalog
install_catalog(app)

MANAGEMENT_ROOT = Path(os.environ.get('MANAGEMENT_ROOT', Path(__file__).resolve().parent / 'management'))
sys.path.insert(0, str(MANAGEMENT_ROOT))
from management_api import install_management
install_management(app, root=Path(os.environ.get('MANAGEMENT_DATA', MANAGEMENT_ROOT / 'private')))
