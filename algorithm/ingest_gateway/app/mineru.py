"""Convert office files to Markdown through the MinerU API."""
import re
from pathlib import Path

import httpx

from app.config import settings

_IMAGE = re.compile(r"!\[[^\]]*\]\([^)]*\)|<img\b[^>]*>", re.IGNORECASE)
_BLANK_RUNS = re.compile(r"\n{3,}")


class MineruError(Exception):
    def __init__(self, message: str, transient: bool):
        super().__init__(message)
        self.transient = transient


def _url(path):
    return settings()["mineru"]["base_url"] + path


def health():
    with httpx.Client(timeout=5, trust_env=False) as c:
        c.get(_url("/health")).raise_for_status()


def to_markdown(path: Path, send_name: str) -> str:
    form = {"backend": "pipeline", "parse_method": "auto", "lang_list": "ch", "formula_enable": "true",
            "table_enable": "true", "return_md": "true", "return_images": "false", "return_middle_json": "false",
            "return_content_list": "false", "response_format_zip": "false"}
    try:
        with httpx.Client(timeout=settings()["mineru"]["timeout_seconds"], trust_env=False) as c, path.open("rb") as f:
            r = c.post(_url("/file_parse"), data=form, files={"files": (send_name, f)})
    except httpx.TransportError as e:
        raise MineruError(f"MinerU 连接失败：{type(e).__name__}", transient=True)
    if r.status_code >= 500:
        raise MineruError(f"MinerU 返回 HTTP {r.status_code}", transient=True)
    try:
        body = r.json()
    except ValueError:
        body = {}
    if r.status_code != 200:
        reason = body.get("error") or body.get("detail") or r.text[:200]
        raise MineruError(f"MinerU 无法解析该文件：{reason}", transient=False)
    results = body.get("results") or {}
    markdown = next(iter(results.values()), {}).get("md_content") if results else None
    if body.get("status") not in (None, "completed") or not markdown or not markdown.strip():
        raise MineruError(f"MinerU 没有返回内容：{body.get('error') or body.get('status')}", transient=False)
    # Image files are not stored, so links to them would point nowhere.
    return _BLANK_RUNS.sub("\n\n", _IMAGE.sub("", markdown)).strip() + "\n"
