import io
from pathlib import Path
import zipfile

import pytest
import yaml

from app.routing import ROUTE_DIRECT, ROUTE_MINERU_MD, ROUTE_RAGFLOW_MINERU, UnsupportedFormat, detect_format, plan

EXAMPLE_CONFIG = Path(__file__).resolve().parents[1] / "config/ingest.example.yaml"
EXAMPLE_SETTINGS = yaml.safe_load(EXAMPLE_CONFIG.read_text(encoding="utf-8"))
CHUNK_METHODS = EXAMPLE_SETTINGS["chunk_method"]


def write(tmp_path, name, data):
    p = tmp_path / name
    p.write_bytes(data)
    return p


def office(part):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("[Content_Types].xml", "<x/>")
        z.writestr(part, "<x/>")
    return buf.getvalue()


@pytest.mark.parametrize("name,data,expected", [
    ("a.pdf", b"%PDF-1.7\n...", "pdf"),
    ("renamed.bin", b"%PDF-1.4\n...", "pdf"),
    ("a.docx", office("word/document.xml"), "docx"),
    ("a.pptx", office("ppt/presentation.xml"), "pptx"),
    ("a.xlsx", office("xl/workbook.xml"), "xlsx"),
    ("a.md", "# 标题\n正文".encode(), "md"),
    ("a.TXT", "纯文本".encode(), "txt"),
])
def test_detect_supported(tmp_path, name, data, expected):
    assert detect_format(write(tmp_path, name, data), name) == expected


@pytest.mark.parametrize("name,data,reason", [
    ("a.doc", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\0" * 64, "旧版 Office"),
    ("a.png", b"\x89PNG\r\n\x1a\n" + b"\0" * 64, "图片"),
    ("a.jpg", b"\xff\xd8\xff\xe0" + b"\0" * 64, "图片"),
    ("a.zip", office("other/file.xml"), "压缩包"),
    ("a.csv", b"a,b\n1,2\n", "csv"),
    ("a.md", b"\xff\xfe\x00\x00binary\x00", "md"),
])
def test_detect_rejected(tmp_path, name, data, reason):
    with pytest.raises(UnsupportedFormat, match=reason):
        detect_format(write(tmp_path, name, data), name)


def test_utf8_cut_at_sample_end_is_text(tmp_path):
    data = ("x" * 8190 + "汉字").encode()  # third byte of 汉 sits past the 8 KiB sample
    assert detect_format(write(tmp_path, "a.md", data), "a.md") == "md"


@pytest.mark.parametrize("fmt,category,route,method", [
    ("pdf", "paper", ROUTE_RAGFLOW_MINERU, "paper"),
    ("pdf", "standard", ROUTE_RAGFLOW_MINERU, "laws"),
    ("pdf", "general", ROUTE_RAGFLOW_MINERU, "naive"),
    ("docx", "paper", ROUTE_MINERU_MD, "naive"),
    ("docx", "policy", ROUTE_MINERU_MD, "laws"),
    ("md", "manual", ROUTE_DIRECT, "naive"),
    ("txt", "standard", ROUTE_DIRECT, "laws"),
])
def test_plan(fmt, category, route, method):
    r = plan(fmt, category, CHUNK_METHODS)
    assert (r.route, r.chunk_method) == (route, method)


def test_every_category_has_a_template():
    categories = EXAMPLE_SETTINGS["categories"]
    for kind in ("pdf", "markdown"):
        assert set(CHUNK_METHODS[kind]) == set(categories)
