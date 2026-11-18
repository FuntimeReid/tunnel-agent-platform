#!/usr/bin/env python3
"""Submit a PDF to the local PaddleOCR-VL service and atomically save JSON."""

import argparse
import base64
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import time

import requests


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--base-url", default=os.getenv("PADDLEOCR_SERVICE_URL", "http://127.0.0.1:18888"))
    parser.add_argument("--timeout", type=int, default=int(os.getenv("PADDLEOCR_CLIENT_TIMEOUT_SECONDS", "14400")))
    args = parser.parse_args()

    data = args.input.read_bytes()
    if not data.startswith(b"%PDF-"):
        raise ValueError(f"Input is not a PDF: {args.input}")

    client = requests.Session()
    client.trust_env = False
    client.get(f"{args.base_url.rstrip('/')}/health", timeout=10).raise_for_status()

    payload = {
        "file": base64.b64encode(data).decode("ascii"),
        "fileType": 0,
        "returnMarkdownImages": False,
        "visualize": False,
        "prettifyMarkdown": True,
        "showFormulaNumber": True,
        "restructurePages": True,
        "mergeTables": True,
        "relevelTitles": True,
    }
    started = time.monotonic()
    response = client.post(
        f"{args.base_url.rstrip('/')}/layout-parsing",
        json=payload,
        timeout=(15, args.timeout),
    )
    response.raise_for_status()
    result = response.json()
    if result.get("errorCode") != 0:
        raise RuntimeError(f"PaddleOCR failed: {result.get('errorMsg')}")

    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_name(args.output.name + ".tmp")
    temporary.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(args.output)

    pages = result.get("result", {}).get("layoutParsingResults", [])
    blocks = [block for page in pages for block in page.get("prunedResult", {}).get("parsing_res_list", [])]
    print(json.dumps({
        "elapsed_seconds": round(time.monotonic() - started, 1),
        "source_sha256": hashlib.sha256(data).hexdigest(),
        "pages": len(pages),
        "layout_blocks": len(blocks),
        "layout_types": Counter(block.get("block_label", "") for block in blocks),
        "markdown_characters": sum(len(page.get("markdown", {}).get("text") or "") for page in pages),
        "response_file": str(args.output),
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
