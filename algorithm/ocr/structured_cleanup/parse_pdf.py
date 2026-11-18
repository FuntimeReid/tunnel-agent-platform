#!/usr/bin/env python3
"""Parse all nine original pages through a local MinerU HTTP API."""

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import zipfile

import requests


ROOT = Path(__file__).resolve().parent.parent
OUT = Path(__file__).resolve().parent
SOURCE = ROOT / "test ocr/2508.11247v1.pdf"
API = "http://127.0.0.1:18887"


def save_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def main():
    raw = OUT / "raw"
    raw.mkdir(exist_ok=True)
    if (raw / "response.zip").exists():
        raise FileExistsError("Preserve the previous raw response before starting another parse")
    session = requests.Session()
    session.trust_env = False
    process = None
    log = None
    try:
        try:
            health = session.get(API + "/health", timeout=2)
            health.raise_for_status()
        except requests.RequestException:
            env = os.environ.copy()
            env.update(
                MINERU_TOOLS_CONFIG_JSON=str(ROOT / ".tools/mineru-home/mineru.json"),
                MINERU_MODEL_SOURCE="modelscope", MINERU_DEVICE_MODE="cpu",
                HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1",
                OMP_NUM_THREADS="2", OPENBLAS_NUM_THREADS="2",
                MINERU_API_OUTPUT_ROOT=str(raw / "service-output"),
            )
            log = (raw / "service.log").open("a")
            process = subprocess.Popen(
                [sys.executable, "-m", "mineru.cli.fast_api", "--host", "127.0.0.1", "--port", "18887"],
                env=env, stdout=log, stderr=subprocess.STDOUT,
            )
            for _ in range(60):
                if process.poll() is not None:
                    raise RuntimeError("MinerU startup failed; see raw/service.log")
                try:
                    health = session.get(API + "/health", timeout=2)
                    health.raise_for_status()
                    break
                except requests.RequestException:
                    time.sleep(1)
            else:
                raise RuntimeError("MinerU startup timed out; see raw/service.log")
        save_json(raw / "health.json", health.json())
        schema = session.get(API + "/openapi.json", timeout=5)
        schema.raise_for_status()
        (raw / "openapi.json").write_bytes(schema.content)
        fields = dict(
            backend="pipeline", parse_method="auto", lang_list="en",
            formula_enable="true", table_enable="true", return_md="true",
            return_middle_json="true", return_model_output="true", return_content_list="true",
            return_images="false", response_format_zip="true",
        )
        metadata = dict(
            created_at_utc=datetime.now(timezone.utc).isoformat(), endpoint=API + "/file_parse",
            source=str(SOURCE.relative_to(ROOT)), source_sha256=hashlib.sha256(SOURCE.read_bytes()).hexdigest(),
            fields=fields, scope="complete original PDF, all 9 pages", started_local_service=process is not None,
            script_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        )
        save_json(raw / "request.json", metadata)
        print(f"POST {API}/file_parse; all 9 original pages", flush=True)
        started = time.monotonic()
        with SOURCE.open("rb") as stream:
            response = session.post(
                API + "/file_parse", data=fields,
                files={"files": (SOURCE.name, stream, "application/pdf")}, timeout=(5, 1200),
            )
        metadata.update(status_code=response.status_code, elapsed_seconds=round(time.monotonic() - started, 3))
        save_json(raw / "response-metadata.json", metadata)
        if not response.ok:
            (raw / "http-error-body.bin").write_bytes(response.content)
            response.raise_for_status()
        (raw / "response.zip").write_bytes(response.content)
        with zipfile.ZipFile(raw / "response.zip") as archive:
            for name in archive.namelist():
                if not (raw / name).resolve().is_relative_to(raw.resolve()):
                    raise ValueError("Unsafe archive path")
            for member in archive.infolist():
                if Path(member.filename).suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}:
                    archive.extract(member, raw)
        print(f"HTTP {response.status_code}; elapsed={metadata['elapsed_seconds']}s; saved to {raw}", flush=True)
    except requests.RequestException as exc:
        (raw / "request-error.txt").write_text(f"{type(exc).__name__}: {exc}\n")
        raise
    finally:
        if process:
            process.terminate()
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        if log:
            log.close()


if __name__ == "__main__":
    main()
