"""Ingest gateway HTTP API. nginx forwards /ingest/* here unchanged."""
import contextvars
import hashlib
import logging
import re
import shutil
import threading
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, Form, Header, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

from app import db, mineru, ragflow, worker
from app.config import library_parser_config, settings
from app.routing import UnsupportedFormat, detect_format, plan

_request_id = contextvars.ContextVar("request_id", default="-")


class _RequestIdFilter(logging.Filter):
    def filter(self, record):
        record.request_id = _request_id.get()
        return True


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s request_id=%(request_id)s %(message)s")
for _h in logging.getLogger().handlers:
    _h.addFilter(_RequestIdFilter())
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("ingest.api")

PUBLIC_STATUS = {worker.RECEIVED: "queued", worker.CONVERTING: "queued", worker.UPLOADING: "queued",
                 worker.PARSING: "parsing", worker.DONE: "done", worker.FAILED: "failed"}
_ID = re.compile(r"^[0-9a-f]{32}$")
_create_lock = threading.Lock()


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str, data=None):
        super().__init__(message)
        self.status, self.code, self.message, self.data = status, code, message, data


def ok(data, status=200):
    return JSONResponse({"code": 0, "request_id": _request_id.get(), "data": data}, status_code=status)


def error_response(status, code, message, data=None):
    body = {"code": code, "message": message, "request_id": _request_id.get()}
    if data is not None:
        body["data"] = data
    return JSONResponse(body, status_code=status)


@asynccontextmanager
async def lifespan(_app):
    Path(settings()["storage"]["staging_dir"]).mkdir(parents=True, exist_ok=True)
    worker.start()
    yield
    worker.stop()


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


@app.middleware("http")
async def request_context(request: Request, call_next):
    rid = uuid.uuid4().hex[:16]
    token = _request_id.set(rid)
    try:
        response = await call_next(request)
    except Exception:  # noqa: BLE001 - answer in our envelope, keep the request_id
        log.exception("unhandled error on %s %s", request.method, request.url.path)
        response = error_response(500, "internal_error", "服务端内部错误，请把 request_id 发给服务端排查")
    finally:
        _request_id.reset(token)
    response.headers["X-Request-Id"] = rid
    return response


@app.exception_handler(ApiError)
async def _api_error(_req, e: ApiError):
    return error_response(e.status, e.code, e.message, e.data)


@app.exception_handler(RequestValidationError)
async def _validation_error(_req, e: RequestValidationError):
    first = e.errors()[0] if e.errors() else {}
    where = ".".join(str(x) for x in first.get("loc", []) if x not in ("body", "query", "path"))
    return error_response(400, "invalid_request", f"参数不正确：{where or '请求体'} {first.get('msg', '')}".strip())


@app.exception_handler(StarletteHTTPException)
async def _http_error(_req, e: StarletteHTTPException):
    code = {404: "not_found", 405: "method_not_allowed"}.get(e.status_code, "http_error")
    return error_response(e.status_code, code, "接口不存在" if e.status_code == 404 else str(e.detail))


@app.exception_handler(ragflow.RagflowError)
async def _ragflow_error(_req, e: ragflow.RagflowError):
    log.warning("ragflow error: %s", e)
    if e.transient:
        return error_response(503, "upstream_unavailable", "RAGFlow 暂时不可用，请稍后重试")
    return error_response(502, "upstream_error", f"RAGFlow 返回错误：{e}")


@app.exception_handler(db.OperationalError)
async def _db_error(_req, e):
    log.warning("registry unavailable: %s", e)
    return error_response(503, "upstream_unavailable", "登记库暂时不可用，请稍后重试")


def caller_id(value: str | None) -> str:
    return (value or "").strip()[:128] or "anonymous"


def record_view(rec: dict) -> dict:
    error = None
    if rec["stage"] == worker.FAILED:
        error = {"code": rec["error_code"], "message": rec["error_message"]}
    return {
        "record_id": rec["id"], "library_id": rec["library_id"], "filename": rec["filename"],
        "format": rec["format"], "category": rec["category"], "route": rec["route"], "chunk_method": rec["chunk_method"],
        "status": PUBLIC_STATUS[rec["stage"]], "stage": rec["stage"], "progress": round(rec["progress"], 3),
        "chunk_count": rec["chunk_count"], "doc_id": rec["ragflow_doc_id"], "error": error,
        # A transient upstream problem being retried: still queued, but say why it is waiting.
        "retry_note": rec["error_message"] if rec["stage"] != worker.FAILED and rec["next_attempt_at"] else None,
        "created_at": db.iso(rec["created_at"]), "updated_at": db.iso(rec["updated_at"]),
    }


def require_id(value: str, what: str) -> str:
    if not _ID.match(value or ""):
        raise ApiError(400, "invalid_request", f"{what} 格式不正确，应为 32 位十六进制字符串")
    return value


@app.get("/ingest/health")
def health():
    checks = {}
    for name, probe in (("registry", lambda: db.one("SELECT 1 AS ok")), ("ragflow", ragflow.health), ("mineru", mineru.health)):
        try:
            probe()
            checks[name] = "ok"
        except Exception as e:  # noqa: BLE001 - report which dependency is down
            checks[name] = f"error: {type(e).__name__}"
    healthy = all(v == "ok" for v in checks.values())
    data = {"status": "ok" if healthy else "degraded", "checks": checks}
    return ok(data) if healthy else error_response(503, "upstream_unavailable", "部分依赖不可用", data)


@app.get("/ingest/libraries")
def list_libraries():
    rows = db.query(
        "SELECT l.id, l.name, l.description, l.created_at, COUNT(r.id) AS document_count, "
        "SUM(r.stage='done') AS done_count FROM library l LEFT JOIN ingest_record r ON r.library_id=l.id "
        "GROUP BY l.id ORDER BY l.created_at")
    return ok({"libraries": [{"id": r["id"], "name": r["name"], "description": r["description"],
                              "document_count": int(r["document_count"]), "done_count": int(r["done_count"] or 0),
                              "created_at": db.iso(r["created_at"])} for r in rows]})


class LibraryIn(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    description: str = Field(default="", max_length=1024)


@app.post("/ingest/libraries")
def create_library(body: LibraryIn, x_caller_id: str | None = Header(default=None)):
    name = body.name.strip()
    if not name:
        raise ApiError(400, "invalid_request", "库名称不能为空")
    with _create_lock:
        existing = db.one("SELECT id, name FROM library WHERE name=%s", [name])
        if existing:
            return ok({"id": existing["id"], "name": existing["name"], "existed": True})
        # Adopt a dataset left behind by a crash between the RAGFlow call and the insert.
        ds = ragflow.find_dataset(name) or ragflow.create_dataset(name, body.description, library_parser_config())
        db.execute("INSERT INTO library (id, name, description, created_by, created_at) VALUES (%s,%s,%s,%s,%s)",
                   [ds["id"], name, body.description, caller_id(x_caller_id), db.now()])
    log.info("library created id=%s name=%s caller=%s", ds["id"], name, caller_id(x_caller_id))
    return ok({"id": ds["id"], "name": name, "existed": False}, status=201)


def _stage_upload(upload: UploadFile, record_id: str) -> tuple[Path, str, int]:
    """Copy the upload to staging while hashing. Raises ApiError when too large."""
    limit = settings()["limits"]["max_file_bytes"]
    target = worker.source_path(record_id)
    target.parent.mkdir(parents=True, exist_ok=True)
    digest, size = hashlib.sha256(), 0
    with target.open("wb") as out:
        while chunk := upload.file.read(1024 * 1024):
            size += len(chunk)
            if size > limit:
                out.close()
                shutil.rmtree(target.parent, ignore_errors=True)
                raise ApiError(413, "file_too_large", f"单个文件不能超过 {limit // 1048576} MB")
            digest.update(chunk)
            out.write(chunk)
    return target, digest.hexdigest(), size


def _accept(upload: UploadFile, library_id: str, category: str, caller: str) -> dict:
    filename = Path(upload.filename or "unnamed").name[:255]
    record_id = uuid.uuid4().hex
    try:
        path, sha, size = _stage_upload(upload, record_id)
    except ApiError as e:
        return {"filename": filename, "error": {"code": e.code, "message": e.message}}
    try:
        if size == 0:
            raise UnsupportedFormat("文件为空")
        fmt = detect_format(path, filename)
    except UnsupportedFormat as e:
        shutil.rmtree(path.parent, ignore_errors=True)
        return {"filename": filename, "error": {"code": "unsupported_format", "message": str(e)}}

    existing = db.one("SELECT * FROM ingest_record WHERE library_id=%s AND sha256=%s", [library_id, sha])
    if existing is None:
        route = plan(fmt, category, settings()["chunk_method"])
        ts = db.now()
        try:
            db.execute(
                "INSERT INTO ingest_record (id, library_id, caller_id, filename, size_bytes, sha256, format, category, "
                "chunk_method, route, stage, created_at, updated_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                [record_id, library_id, caller, filename, size, sha, fmt, category, route.chunk_method, route.route,
                 worker.RECEIVED, ts, ts])
            log.info("record=%s accepted file=%s format=%s route=%s template=%s caller=%s",
                     record_id, filename, fmt, route.route, route.chunk_method, caller)
            return {"filename": filename, "record_id": record_id, "status": "queued", "duplicate": False}
        except db.IntegrityError:  # the same file arrived concurrently
            existing = db.one("SELECT * FROM ingest_record WHERE library_id=%s AND sha256=%s", [library_id, sha])

    # Same content already in this library: return that record; a failed one is retried.
    if existing["stage"] == worker.FAILED:
        if not worker.source_path(existing["id"]).exists() and not existing["ragflow_doc_id"]:
            worker.staging_dir(existing["id"]).mkdir(parents=True, exist_ok=True)
            shutil.move(str(path), worker.source_path(existing["id"]))
        worker.retry(existing)
        existing = db.one("SELECT * FROM ingest_record WHERE id=%s", [existing["id"]])
        log.info("record=%s re-uploaded after failure, retrying", existing["id"])
    shutil.rmtree(worker.staging_dir(record_id), ignore_errors=True)
    return {"filename": filename, "record_id": existing["id"], "status": PUBLIC_STATUS[existing["stage"]], "duplicate": True}


@app.post("/ingest/documents")
def upload_documents(library_id: str = Form(...), file: list[UploadFile] = File(...), category: str = Form(default="general"),
                     x_caller_id: str | None = Header(default=None)):
    limits = settings()["limits"]
    if len(file) > limits["max_files_per_request"]:
        raise ApiError(413, "file_too_large", f"单次最多上传 {limits['max_files_per_request']} 个文件")
    require_id(library_id, "library_id")
    if not db.one("SELECT id FROM library WHERE id=%s", [library_id]):
        raise ApiError(404, "library_not_found", "库不存在")
    category = (category or "general").strip() or "general"
    if category not in settings()["categories"]:
        raise ApiError(400, "invalid_request", f"category 只能是：{', '.join(settings()['categories'])}")
    items = [_accept(f, library_id, category, caller_id(x_caller_id)) for f in file]
    if any("record_id" in i for i in items):
        return ok({"items": items}, status=202)
    first = items[0]["error"]
    return error_response(400 if first["code"] != "file_too_large" else 413, first["code"], first["message"], {"items": items})


@app.get("/ingest/documents")
def get_records(ids: str = ""):
    wanted = [require_id(i.strip(), "ids") for i in ids.split(",") if i.strip()]
    if not wanted:
        raise ApiError(400, "invalid_request", "请用 ids 参数传入记录 ID，逗号分隔")
    if len(wanted) > settings()["limits"]["max_status_ids"]:
        raise ApiError(400, "invalid_request", f"一次最多查询 {settings()['limits']['max_status_ids']} 条")
    rows = {r["id"]: r for r in db.query(f"SELECT * FROM ingest_record WHERE id IN ({','.join(['%s'] * len(wanted))})", wanted)}
    return ok({"records": [record_view(rows[i]) for i in wanted if i in rows],
               "missing": [i for i in wanted if i not in rows]})


@app.get("/ingest/documents/{record_id}")
def get_record(record_id: str):
    rec = db.one("SELECT * FROM ingest_record WHERE id=%s", [require_id(record_id, "record_id")])
    if not rec:
        raise ApiError(404, "record_not_found", "入库记录不存在")
    return ok(record_view(rec))


@app.post("/ingest/documents/{record_id}/retry")
def retry_record(record_id: str):
    rec = db.one("SELECT * FROM ingest_record WHERE id=%s", [require_id(record_id, "record_id")])
    if not rec:
        raise ApiError(404, "record_not_found", "入库记录不存在")
    try:
        worker.retry(rec)
    except worker.NotRetryable as e:
        raise ApiError(409, "not_retryable", str(e))
    log.info("record=%s retry requested", record_id)
    return ok(record_view(db.one("SELECT * FROM ingest_record WHERE id=%s", [record_id])))