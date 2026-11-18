"""Background processing: convert -> upload -> parse, then follow RAGFlow until done.

Two threads: one moves records through convert/upload (one at a time, matching
MinerU's single slot), one polls RAGFlow for records already parsing.
"""
import logging
import shutil
import threading
import time
from datetime import timedelta
from pathlib import Path

from app import db, mineru, ragflow
from app.config import library_parser_config, settings
from app.routing import ROUTE_MINERU_MD

log = logging.getLogger("ingest.worker")
_stop = threading.Event()

RECEIVED, CONVERTING, UPLOADING, PARSING, DONE, FAILED = "received", "converting", "uploading", "parsing", "done", "failed"


class NotRetryable(Exception):
    pass


def staging_dir(record_id: str) -> Path:
    return Path(settings()["storage"]["staging_dir"]) / record_id


def source_path(record_id: str) -> Path:
    return staging_dir(record_id) / "source"


def converted_path(record_id: str) -> Path:
    return staging_dir(record_id) / "converted.md"


def fail(rec_id: str, code: str, message: str):
    log.warning("record=%s failed %s: %s", rec_id, code, message)
    db.update_record(rec_id, stage=FAILED, error_code=code, error_message=message[:2000], next_attempt_at=None)


def _transient(rec: dict, back_to: str, code: str, message: str):
    delays = settings()["worker"]["transient_retry_delays_seconds"]
    if rec["attempts"] >= len(delays):
        fail(rec["id"], code, f"{message}（已重试 {len(delays)} 次）")
        return
    wait = delays[rec["attempts"]]
    log.info("record=%s transient %s, retry in %ss: %s", rec["id"], code, wait, message)
    db.update_record(rec["id"], stage=back_to, attempts=rec["attempts"] + 1, error_message=message[:2000],
                     next_attempt_at=db.now() + timedelta(seconds=wait))


def _next_record() -> dict | None:
    return db.one(
        "SELECT * FROM ingest_record WHERE stage IN (%s, %s) AND (next_attempt_at IS NULL OR next_attempt_at <= %s) "
        "ORDER BY created_at LIMIT 1", [RECEIVED, UPLOADING, db.now()])


def _upload_name(rec: dict) -> str:
    if rec["route"] == ROUTE_MINERU_MD:
        return Path(rec["filename"]).stem + ".md"
    return rec["filename"]


def process(rec: dict):
    rid = rec["id"]
    step = rec["stage"]
    try:
        if step == RECEIVED:
            if rec["route"] == ROUTE_MINERU_MD:
                step = CONVERTING
                db.update_record(rid, stage=CONVERTING)
                send_name = Path(rec["filename"]).stem + "." + rec["format"]
                converted_path(rid).write_text(mineru.to_markdown(source_path(rid), send_name), encoding="utf-8")
            step = UPLOADING
            db.update_record(rid, stage=UPLOADING, attempts=0, next_attempt_at=None, error_message=None)
            rec = db.one("SELECT * FROM ingest_record WHERE id=%s", [rid])

        doc_id = rec["ragflow_doc_id"]
        if not doc_id:
            path = converted_path(rid) if rec["route"] == ROUTE_MINERU_MD else source_path(rid)
            if not path.exists():
                fail(rid, "upload_failed", "暂存文件已丢失，请重新上传")
                return
            doc_id = ragflow.upload(rec["library_id"], path, _upload_name(rec))["id"]
            db.update_record(rid, ragflow_doc_id=doc_id)
            log.info("record=%s uploaded doc=%s", rid, doc_id)

        if rec["chunk_method"] != settings()["library_defaults"]["chunk_method"]:
            ragflow.set_template(rec["library_id"], doc_id, rec["chunk_method"], library_parser_config())
        # Resume-safe: a crash after the parse call must not start a second parse.
        current = ragflow.document(rec["library_id"], doc_id)
        if current is None:
            fail(rid, "upload_failed", "RAGFlow 中找不到刚上传的文档")
            return
        if current["run"] not in ("RUNNING", ragflow.DONE):
            ragflow.parse(rec["library_id"], doc_id)
        db.update_record(rid, stage=PARSING, progress=0, attempts=0, next_attempt_at=None, error_code=None, error_message=None)
        shutil.rmtree(staging_dir(rid), ignore_errors=True)
        log.info("record=%s parsing doc=%s template=%s", rid, doc_id, rec["chunk_method"])
    except mineru.MineruError as e:
        (_transient(rec, RECEIVED, "convert_failed", str(e)) if e.transient else fail(rid, "convert_failed", str(e)))
    except ragflow.RagflowError as e:
        code = "upload_failed"
        (_transient(rec, UPLOADING if step == UPLOADING else RECEIVED, code, str(e)) if e.transient else fail(rid, code, str(e)))
    except Exception as e:  # noqa: BLE001 - one bad record must not stop the loop
        log.exception("record=%s unexpected error in %s", rid, step)
        fail(rid, "convert_failed" if step == CONVERTING else "upload_failed", f"内部错误：{type(e).__name__}: {e}")


def sync_once():
    for rec in db.query("SELECT * FROM ingest_record WHERE stage=%s ORDER BY updated_at LIMIT 200", [PARSING]):
        try:
            d = ragflow.document(rec["library_id"], rec["ragflow_doc_id"])
        except ragflow.RagflowError as e:
            if not e.transient:
                fail(rec["id"], "parse_failed", f"无法查询 RAGFlow 文档状态：{e}")
            continue  # transient: next round
        if d is None:
            fail(rec["id"], "parse_failed", "RAGFlow 中的文档已不存在")
        elif d["run"] == ragflow.DONE and (d.get("progress") or 0) >= 1:
            db.update_record(rec["id"], stage=DONE, progress=1, chunk_count=d.get("chunk_count") or 0)
            log.info("record=%s done chunks=%s", rec["id"], d.get("chunk_count"))
        elif d["run"] in (ragflow.FAILED, ragflow.CANCELLED) or (d.get("progress") or 0) < 0:
            lines = [ln for ln in (d.get("progress_msg") or "").splitlines() if ln.strip()]
            fail(rec["id"], "parse_failed", (lines[-1] if lines else "RAGFlow 解析失败")[:500])
        elif abs((d.get("progress") or 0) - rec["progress"]) >= 0.01:
            db.update_record(rec["id"], progress=round(d.get("progress") or 0, 3))


def retry(rec: dict):
    """Put a failed record back in the queue. Raises NotRetryable when it cannot be redone."""
    if rec["stage"] != FAILED:
        raise NotRetryable("只有失败的记录才能重试")
    if rec["ragflow_doc_id"]:
        # The document is already in RAGFlow; re-trigger the template + parse step.
        # RAGFlow clears the old chunks before re-parsing.
        stage = UPLOADING
    elif source_path(rec["id"]).exists():
        stage = RECEIVED
    else:
        raise NotRetryable("暂存文件已清理，请重新上传该文件")
    db.update_record(rec["id"], stage=stage, attempts=0, next_attempt_at=None, error_code=None, error_message=None, progress=0)


def recover():
    """On start: a conversion cut off by a restart is redone from the source file."""
    n = db.execute("UPDATE ingest_record SET stage=%s, updated_at=%s WHERE stage=%s", [RECEIVED, db.now(), CONVERTING])
    if n:
        log.info("recovered %s interrupted conversions", n)


def cleanup_staging():
    root = Path(settings()["storage"]["staging_dir"])
    keep_days = settings()["storage"]["failed_staging_retention_days"]
    cutoff = db.now() - timedelta(days=keep_days)
    for d in root.iterdir() if root.exists() else []:
        rec = db.one("SELECT stage, updated_at FROM ingest_record WHERE id=%s", [d.name])
        orphan = rec is None and time.time() - d.stat().st_mtime > 3600
        finished = rec is not None and rec["stage"] in (PARSING, DONE)
        expired = rec is not None and rec["stage"] == FAILED and rec["updated_at"] < cutoff
        if orphan or finished or expired:
            shutil.rmtree(d, ignore_errors=True)
            log.info("staging removed %s (%s)", d.name, "orphan" if orphan else "finished" if finished else "expired")


def _process_loop():
    while not _stop.is_set():
        try:
            rec = _next_record()
        except Exception:  # noqa: BLE001 - registry briefly unreachable
            log.exception("process loop: cannot read registry")
            _stop.wait(10)
            continue
        if rec is None:
            _stop.wait(2)
            continue
        process(rec)


def _sync_loop():
    interval = settings()["worker"]["sync_interval_seconds"]
    last_cleanup = 0.0
    while not _stop.is_set():
        try:
            sync_once()
            if time.monotonic() - last_cleanup > 3600:
                cleanup_staging()
                last_cleanup = time.monotonic()
        except Exception:  # noqa: BLE001
            log.exception("sync loop error")
        _stop.wait(interval)


def start():
    for attempt in range(30):
        try:
            recover()
            break
        except Exception:  # noqa: BLE001 - MySQL may still be starting after a reboot
            log.warning("registry not reachable yet, retry %s/30", attempt + 1)
            time.sleep(5)
    for target in (_process_loop, _sync_loop):
        threading.Thread(target=target, name=target.__name__, daemon=True).start()


def stop():
    _stop.set()
