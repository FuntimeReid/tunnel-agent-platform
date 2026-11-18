"""Decide how a file is processed: detect format from bytes, pick route and template."""
import zipfile
from dataclasses import dataclass
from pathlib import Path

ROUTE_RAGFLOW_MINERU = "ragflow_mineru"  # PDF: RAGFlow calls MinerU, keeps page positions
ROUTE_MINERU_MD = "mineru_md"  # office: we convert to Markdown first
ROUTE_DIRECT = "direct"  # md / txt uploaded as-is

OFFICE_PARTS = {"word/": "docx", "ppt/": "pptx", "xl/": "xlsx"}
TEXT_SUFFIXES = {".md": "md", ".markdown": "md", ".txt": "txt"}


class UnsupportedFormat(ValueError):
    pass


@dataclass(frozen=True)
class Route:
    format: str
    route: str
    chunk_method: str


def detect_format(path: Path, filename: str) -> str:
    with path.open("rb") as f:
        head = f.read(8192)
    if head.startswith(b"%PDF-"):
        return "pdf"
    if head.startswith(b"PK\x03\x04"):
        try:
            with zipfile.ZipFile(path) as z:
                names = z.namelist()
        except zipfile.BadZipFile:
            raise UnsupportedFormat("压缩包已损坏")
        for prefix, fmt in OFFICE_PARTS.items():
            if any(n.startswith(prefix) for n in names):
                return fmt
        raise UnsupportedFormat("不支持的压缩包，只接受 docx、pptx、xlsx")
    if head.startswith(b"\xd0\xcf\x11\xe0"):
        raise UnsupportedFormat("不支持旧版 Office 格式（doc、ppt、xls），请另存为 docx、pptx、xlsx")
    if head[:3] == b"\xff\xd8\xff" or head.startswith((b"\x89PNG", b"GIF8", b"BM", b"II*\x00", b"MM\x00*")) or head[8:12] == b"WEBP":
        raise UnsupportedFormat("不支持图片文件")
    fmt = TEXT_SUFFIXES.get(Path(filename).suffix.lower())
    if fmt and _is_utf8(head):
        return fmt
    raise UnsupportedFormat(f"不支持的文件格式：{Path(filename).suffix.lstrip('.') or '无扩展名'}")


def _is_utf8(head: bytes) -> bool:
    try:
        head.decode("utf-8")
        return True
    except UnicodeDecodeError as e:
        # The 8 KiB sample may cut a multi-byte character at the end.
        return e.start >= len(head) - 3


def plan(fmt: str, category: str, chunk_methods: dict) -> Route:
    if fmt == "pdf":
        return Route(fmt, ROUTE_RAGFLOW_MINERU, chunk_methods["pdf"][category])
    route = ROUTE_DIRECT if fmt in ("md", "txt") else ROUTE_MINERU_MD
    return Route(fmt, route, chunk_methods["markdown"][category])
