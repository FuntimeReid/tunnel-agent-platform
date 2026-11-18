#!/usr/bin/env python3
"""Export this paper's chunks using the repository's native Markdown chunk path.

Load the native definitions without importing the PDF, database and model stack.
Only full-text index token fields are omitted; parsing, table serialization,
token counting and chunk boundaries use the repository implementations.
"""

import ast
import copy
from datetime import datetime, timezone
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import runpy
import sys
from timeit import default_timer
from types import SimpleNamespace

DIRECTORY = Path(__file__).resolve().parent
ROOT = Path(os.environ.get("RAGFLOW_ROOT", DIRECTORY.parents[1] / "ragflow"))
sys.path.insert(0, str(ROOT))
cache = ROOT / ".tools/chunk-preview"
cache.mkdir(parents=True, exist_ok=True)
os.environ.setdefault("RAG_PROJECT_BASE", str(cache))

from markdown import markdown  # noqa: E402
from common.float_utils import normalize_overlapped_percent  # noqa: E402
from common.text_utils import normalize_arabic_presentation_forms  # noqa: E402
from common.token_utils import num_tokens_from_string  # noqa: E402
from latex_formulas import validate_latex  # noqa: E402


def load_definitions(relative_path, names, namespace):
    path = ROOT / relative_path
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    selected = [node for node in tree.body
                if isinstance(node, (ast.ClassDef, ast.FunctionDef)) and node.name in names]
    if {node.name for node in selected} != set(names):
        raise ValueError(f"Missing native definitions in {relative_path}")
    exec(compile(ast.Module(body=selected, type_ignores=[]), str(path), "exec"), namespace)


def record_content(document, text, *_args, **_kwargs):
    """Record chunk text without generating unused full-text index token fields."""
    document["content_with_weight"] = text


def no_vision_model(*_args, **_kwargs):
    raise LookupError("Local text-only preview has no tenant/model database")


def compact(value):
    return re.sub(r"\s+", "", value)


def chunk_document(source_bytes, filename="clean.md", settings=None):
    """Run the native Markdown path without model or database dependencies."""
    text = source_bytes.decode("utf-8")
    if re.search(r"!\[|<img\b", text):
        raise ValueError("This text-only preview does not fetch or process images")
    namespace = dict(
        __name__="_ragflow_markdown_preview", copy=copy, re=re, logging=logging,
        logger=logging.getLogger(__name__), markdown=markdown, timer=default_timer,
        MAXIMUM_PAGE_NUMBER=100000, num_tokens_from_string=num_tokens_from_string,
        normalize_overlapped_percent=normalize_overlapped_percent,
        normalize_arabic_presentation_forms=normalize_arabic_presentation_forms,
        tokenize=record_content,
        rag_tokenizer=SimpleNamespace(tokenize=lambda value: value, fine_grained_tokenize=lambda value: value),
        get_tenant_default_model_by_type=no_vision_model,
        LLMType=SimpleNamespace(VISION="image2text"),
    )
    namespace.update(runpy.run_path(str(ROOT / "rag/nlp/delim.py")))
    load_definitions("deepdoc/parser/markdown_parser.py",
                     ["RAGFlowMarkdownParser", "MarkdownElementExtractor"], namespace)
    namespace["MarkdownParser"] = namespace["RAGFlowMarkdownParser"]
    load_definitions("rag/nlp/__init__.py",
                     ["decode_text", "add_positions", "tokenize_table", "tokenize_chunks"], namespace)
    load_definitions("rag/app/naive.py",
                     ["Markdown", "_is_short_header", "_normalize_section_text_for_rtl_presentation_forms", "chunk"], namespace)
    if settings is None:
        settings = dict(chunk_token_num=512, delimiter="\n", overlapped_percent=0,
                        children_delimiter="", analyze_hyperlink=False)
    native_chunks = namespace["chunk"](
        filename, binary=source_bytes, lang="English", parser_config=settings,
        is_root=False, tenant_id="local-markdown-preview", callback=lambda *_args, **_kwargs: None,
    )
    remainder, tables = namespace["RAGFlowMarkdownParser"]().extract_tables_and_remainder(
        text + "\n", separate_tables=True,
    )
    return native_chunks, remainder, tables


def main():
    source = DIRECTORY / "clean.md"
    source_bytes = source.read_bytes()
    text = source_bytes.decode("utf-8")
    settings = dict(chunk_token_num=512, delimiter="\n", overlapped_percent=0,
                    children_delimiter="", analyze_hyperlink=False)
    native_chunks, remainder, tables = chunk_document(source_bytes, source.name, settings)
    table_chunks = [chunk for chunk in native_chunks if chunk.get("doc_type_kwd") == "table"]
    if len(tables) != 6 or len(table_chunks) != len(tables):
        raise ValueError("Expected six independent table chunks; check separate_tables at the Markdown entry")
    chunks = []
    table_number = 0
    for index, native in enumerate(native_chunks, start=1):
        content = native["content_with_weight"]
        kind = native.get("doc_type_kwd", "text")
        display = content
        if kind == "table":
            display = tables[table_number].strip()
            expected = markdown(tables[table_number], extensions=["markdown.extensions.tables"])
            if content != expected:
                raise ValueError(f"Table {table_number + 1} was split or changed")
            table_number += 1
        chunks.append(dict(index=index, doc_type=kind, tokens=num_tokens_from_string(content),
                           characters=len(content), text=content, markdown=display,
                           table_number=table_number if kind == "table" else None,
                           source_pages=None))
    text_chunks = [chunk["text"] for chunk in chunks if chunk["doc_type"] == "text"]
    if compact("".join(text_chunks)) != compact(remainder):
        raise ValueError("Body content was lost, duplicated or reordered")
    source_formulas = validate_latex(text)
    body_formulas = validate_latex(remainder)
    joined_formulas = validate_latex("\n".join(text_chunks))
    if [(f["display"], compact(f["latex"])) for f in body_formulas["formulas"]] != [
            (f["display"], compact(f["latex"])) for f in joined_formulas["formulas"]]:
        raise ValueError("Formula content changed")
    split_formula_chunks = []
    for chunk in chunks:
        try:
            validate_latex(chunk["text"])
        except ValueError:
            split_formula_chunks.append(chunk["index"])
    counts = dict(total=len(chunks), tables=len(tables), text=len(text_chunks),
                  over_512_tokens=sum(chunk["tokens"] > 512 for chunk in chunks),
                  chunks_with_split_math=split_formula_chunks)
    report = dict(
        created_at_utc=datetime.now(timezone.utc).isoformat(), source="clean.md",
        source_sha256=hashlib.sha256(source_bytes).hexdigest(),
        implementation="rag.app.naive.chunk; native Markdown parser; native tokenize_table/tokenize_chunks",
        separate_tables=True, parser_config=settings, token_encoding="cl100k_base", counts=counts,
        omitted="full-text index token fields, embeddings, storage and tenant/model lookups",
        table_preview="original Markdown table; text/tokens/characters describe the native HTML chunk",
        caption_handling="native behavior: captions remain in text chunks",
        source_hashes={name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in (
            "rag/app/naive.py", "deepdoc/parser/markdown_parser.py", "rag/nlp/__init__.py",
            "rag/nlp/delim.py", "common/token_utils.py", "text cor/chunk_markdown.py")},
        checks=dict(tables_complete=True, body_complete=True, formulas_complete=True,
                    inline_formulas=source_formulas["inline_count"], display_formulas=source_formulas["display_count"]),
        chunks=chunks,
    )
    lines = [
        "# clean.md 分块结果", "", "来源：[clean.md](clean.md)。",
        f"共 **{len(chunks)} 块**：正文 **{len(text_chunks)} 块**，表格 **{len(tables)} 块**。", "",
        "策略：`naive`，按换行分段后合并；目标 512 tokens，重叠 0%，`separate_tables=True`。",
        "正文使用现有规则分块，尚未启用向量语义分块。512 tokens 是软目标。", "",
        "表格逐张独立成块，以下用原始 Markdown 表格展示；token 数和字符数按实际输出的 HTML 表格统计。",
        "块顺序沿用原生输出（先表格，后正文）；表题仍在正文块中，尚未自动关联到表格块。",
        "来源页码未推断。完整机器结果见 [chunks.json](chunks.json)。", "",
        f"核验：6 张表完整，正文未丢失、重复或重排，{source_formulas['inline_count']} 处行内公式、"
        f"{source_formulas['display_count']} 个独立公式的内容保留。",
    ]
    if split_formula_chunks:
        lines.extend(["", "当前规则存在公式跨块，涉及块号：" + ", ".join(map(str, split_formula_chunks))
                      + "。这些块用 Markdown 源码展示，避免不闭合的数学定界符影响其他块的预览。"])
    lines.extend(["", "| 块号 | 类型 | Tokens | 字符数 |", "| --- | --- | ---: | ---: |"])
    for chunk in chunks:
        kind = f"表格 {chunk['table_number']}" if chunk["doc_type"] == "table" else "正文"
        lines.append(f"| {chunk['index']:03d} | {kind} | {chunk['tokens']} | {chunk['characters']} |")
    for chunk in chunks:
        kind = f"表格 {chunk['table_number']}" if chunk["doc_type"] == "table" else "正文"
        display = chunk["markdown"]
        if chunk["index"] in split_formula_chunks:
            display = "```markdown\n" + display + "\n```"
        lines.extend(["", "---", "", f"## Chunk {chunk['index']:03d} · {kind}", "",
                      f"{chunk['tokens']} tokens · {chunk['characters']} 字符", "", display])
    if source.read_bytes() != source_bytes:
        raise ValueError("The source changed during chunking")
    (DIRECTORY / "chunks.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (DIRECTORY / "chunks.md").write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    print(json.dumps(counts, ensure_ascii=False))
    print(f"Saved {DIRECTORY / 'chunks.md'}")


if __name__ == "__main__":
    main()
