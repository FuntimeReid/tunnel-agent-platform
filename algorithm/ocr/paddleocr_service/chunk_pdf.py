#!/usr/bin/env python3
"""Chunk a normalized document layout response using title hierarchy."""

import argparse
from collections import Counter
from datetime import datetime
from difflib import SequenceMatcher
import hashlib
import html
import json
import os
import re
from pathlib import Path

import pypdfium2 as pdfium
import tiktoken
from lxml import html as html_parser


IGNORE_LABELS = {"header", "header_image", "footer", "number", "aside_text"}
IMAGE_LABELS = {"image", "chart"}
FORMULA_LABELS = {"display_formula", "formula"}
SENTENCE_RE = re.compile(r"([。!?？；！\n]|\. )")
SPACE_RE = re.compile(r"\s+")
TOKEN_ENCODING = "cl100k_base"
TOKEN_CAP = 512


def norm_heading(text):
    text = re.sub(r"^\s*#+\s*", "", text or "")
    return "".join(re.findall(r"[a-z0-9]+", text.lower()))


def plain_text(text):
    if not text:
        return ""
    try:
        text = html_parser.fromstring(f"<div>{text}</div>").text_content()
    except (ValueError, TypeError):
        text = re.sub(r"<[^>]*>", " ", text)
    return html.unescape(text).replace("\uFFFD", "�").strip()


def reading_column(box, page_width):
    left, _, right, _ = box
    width = right - left
    if width >= page_width * 0.6:
        return "wide"
    return "left" if (left + right) / 2 < page_width / 2 else "right"


def ordered_page_blocks(page, page_number, page_width):
    blocks = page.get("prunedResult", {}).get("parsing_res_list", [])
    order_blocks = [b for b in blocks if b.get("block_order") is not None]
    if not order_blocks:
        return sorted(blocks, key=lambda b: (b.get("block_bbox", [0, 0, 0, 0])[1], b.get("block_bbox", [0, 0, 0, 0])[0]))

    columns = {}
    for block in order_blocks:
        col = reading_column(block.get("block_bbox", [0, 0, 0, 0]), page_width)
        columns.setdefault(col, []).append(block)
    for col_blocks in columns.values():
        col_blocks.sort(key=lambda b: b["block_order"])

    ranked = []
    for block in blocks:
        if block.get("block_order") is not None:
            rank = float(block["block_order"])
        else:
            box = block.get("block_bbox", [0, 0, 0, 0])
            col = reading_column(box, page_width)
            y = (box[1] + box[3]) / 2
            peers = columns.get(col) or order_blocks
            above = [p for p in peers if (p["block_bbox"][1] + p["block_bbox"][3]) / 2 <= y]
            below = [p for p in peers if (p["block_bbox"][1] + p["block_bbox"][3]) / 2 > y]
            if above and below:
                previous = max(above, key=lambda p: p["block_order"])
                following = min(below, key=lambda p: p["block_order"])
                rank = (float(previous["block_order"]) + float(following["block_order"])) / 2
            elif above:
                previous = max(above, key=lambda p: p["block_order"])
                rank = float(previous["block_order"]) + 0.25
            elif below:
                following = min(below, key=lambda p: p["block_order"])
                rank = float(following["block_order"]) - 0.25
            else:
                rank = float(page_number) + y / max(page.get("height", 1), 1)
        ranked.append((rank, block.get("block_bbox", [0, 0, 0, 0])[1], block.get("block_bbox", [0, 0, 0, 0])[0], block))
    return [item[3] for item in sorted(ranked, key=lambda item: item[:3])]


def split_on_sentences(text):
    raw = SENTENCE_RE.split(text)
    out = []
    for index in range(0, len(raw), 2):
        fragment = raw[index]
        if index + 1 < len(raw):
            fragment += raw[index + 1]
        if fragment:
            out.append(fragment)
    return out


def hard_split(text, cap, encoder):
    if not text:
        return []
    out = []
    rest = text
    while rest:
        if len(encoder.encode(rest, disallowed_special=())) <= cap:
            out.append(rest)
            break
        low, high = 1, len(rest)
        while low < high:
            middle = (low + high + 1) // 2
            if len(encoder.encode(rest[:middle], disallowed_special=())) <= cap:
                low = middle
            else:
                high = middle - 1
        cut = low
        safe_space = rest.rfind(" ", 0, cut)
        if safe_space >= cut // 2:
            cut = safe_space + 1
        if cut <= 0:
            cut = low
        out.append(rest[:cut])
        rest = rest[cut:]
    return out


def split_text_at_cap(text, cap, encoder):
    sentences = split_on_sentences(text)
    out, current = [], ""
    for sentence in sentences:
        candidate = current + sentence
        if current and len(encoder.encode(candidate, disallowed_special=())) > cap:
            out.append(current)
            current = ""
        if len(encoder.encode(sentence, disallowed_special=())) > cap:
            if current:
                out.append(current)
                current = ""
            out.extend(hard_split(sentence, cap, encoder))
        else:
            current += sentence
    if current:
        out.append(current)
    return out


def table_info(content):
    tree = html_parser.fromstring(content)
    rows = tree.xpath(".//tr")
    occupied = set()
    for row_index, row in enumerate(rows):
        column = 0
        for cell in row.xpath("./td|./th"):
            while (row_index, column) in occupied:
                column += 1
            row_span = int(cell.get("rowspan", 1))
            col_span = int(cell.get("colspan", 1))
            if row_span < 1 or col_span < 1:
                raise ValueError("Table contains a nonpositive cell span")
            for r in range(row_index, row_index + row_span):
                for c in range(column, column + col_span):
                    if (r, c) in occupied:
                        raise ValueError("Table contains overlapping merged cells")
                    occupied.add((r, c))
            column += col_span
    width = max((c + 1 for _, c in occupied), default=0)
    counts = [sum((r, c) in occupied for c in range(width)) for r in range(len(rows))]
    valid = bool(rows) and all(count == width for count in counts) and all(r < len(rows) for r, _ in occupied)
    cells = [" ".join(cell.text_content().split()) for cell in tree.xpath(".//td|.//th")]
    return {"rows": len(rows), "columns": width, "cells": cells, "row_widths": counts, "structure_valid": valid}


def outline_headings(source):
    pdf = pdfium.PdfDocument(source)
    try:
        outlines = []
        for bookmark in pdf.get_toc():
            destination = bookmark.get_dest()
            outlines.append({"title": bookmark.get_title(), "level": bookmark.level + 1, "page": destination.get_index() + 1})
        return outlines
    finally:
        pdf.close()


def select_outline(text, page, outlines):
    needle = norm_heading(text)
    if not needle:
        return None
    ranked = []
    for outline in outlines:
        if outline["page"] > page:
            continue
        candidate = norm_heading(outline["title"])
        score = SequenceMatcher(None, needle, candidate).ratio()
        if needle in candidate or candidate in needle:
            score = max(score, 0.95)
        ranked.append((score, outline))
    if not ranked:
        return None
    score, outline = max(ranked, key=lambda item: item[0])
    return outline if score >= 0.78 else None


def block_position(block, page):
    bbox = block.get("block_bbox") or [0, 0, 0, 0]
    return [page, *[float(value) for value in bbox[:4]]]


def make_records(response, page_width, outlines):
    pages = response.get("result", {}).get("layoutParsingResults", [])
    records, skipped_images, labels = [], [], Counter()
    for page_number, page in enumerate(pages, start=1):
        ordered = ordered_page_blocks(page, page_number, page_width)
        blocks = [b for b in ordered if b.get("block_label") not in IGNORE_LABELS]

        tables = [b for b in blocks if b.get("block_label") == "table"]
        attached_captions = set()
        table_caption_map = {}
        table_caption_blocks = {}
        table_note_blocks = {}
        captions = [b for b in blocks if b.get("block_label") in {"figure_title", "figure_table_chart_title", "table_title"}]
        for table in tables:
            if "table_caption" in table:
                table_id = table.get("global_block_id")
                table_caption_map[table_id] = "\n".join(table["table_caption"])
                table_caption_blocks[table_id] = []
                table_note_blocks[table_id] = []
                continue
            tb = table.get("block_bbox", [0, 0, 0, 0])
            candidates = []
            for caption in captions:
                cb = caption.get("block_bbox", [0, 0, 0, 0])
                vertical_gap = tb[1] - cb[3]
                horizontal_overlap = min(tb[2], cb[2]) - max(tb[0], cb[0])
                if 0 <= vertical_gap <= 110 and horizontal_overlap > 0:
                    candidates.append((vertical_gap, caption))
            if candidates:
                caption = min(candidates, key=lambda item: item[0])[1]
                table_caption_blocks[table.get("global_block_id")] = [caption]
            number_candidates = []
            for caption in blocks:
                caption_text = plain_text(caption.get("block_content", ""))
                caption_text = re.sub(r"^#+\s*", "", caption_text)
                if not re.match(r"^Table\s+\d+[.:]?\s*$", caption_text, re.IGNORECASE):
                    continue
                cb = caption.get("block_bbox", [0, 0, 0, 0])
                if 0 <= tb[1] - cb[3] <= 125 and min(tb[2], cb[2]) > max(tb[0], cb[0]):
                    number_candidates.append((tb[1] - cb[3], caption))
            if number_candidates:
                number = min(number_candidates, key=lambda item: item[0])[1]
                table_caption_blocks.setdefault(table.get("global_block_id"), []).append(number)
            caption_blocks = sorted(table_caption_blocks.get(table.get("global_block_id"), []), key=lambda b: b["block_bbox"][1])
            table_caption_blocks[table.get("global_block_id")] = caption_blocks
            for caption in caption_blocks:
                attached_captions.add(caption.get("global_block_id"))
            table_caption_map[table.get("global_block_id")] = "\n".join(
                re.sub(r"^#+\s*", "", plain_text(b.get("block_content", ""))) for b in caption_blocks
            )
            if table.get("table_caption"):
                table_caption_map[table.get("global_block_id")] = "\n".join(table["table_caption"])
            notes = []
            for candidate in blocks:
                if candidate.get("block_label") not in {"figure_title", "vision_footnote", "footnote"}:
                    continue
                note_text = plain_text(candidate.get("block_content", ""))
                if not re.match(r"^(?:Notes?\b|The minus sign\b|注[:：])", note_text, re.IGNORECASE):
                    continue
                nb = candidate.get("block_bbox", [0, 0, 0, 0])
                if 0 <= nb[1] - tb[3] <= 25 and min(tb[2], nb[2]) > max(tb[0], nb[0]):
                    notes.append(candidate)
                    attached_captions.add(candidate.get("global_block_id"))
            table_note_blocks[table.get("global_block_id")] = notes

        formulas = [b for b in blocks if b.get("block_label") in FORMULA_LABELS]
        formula_numbers = [b for b in blocks if b.get("block_label") == "formula_number"]
        number_by_formula = {}
        attached_numbers = set()
        for formula in formulas:
            fb = formula.get("block_bbox", [0, 0, 0, 0])
            candidates = []
            for number in formula_numbers:
                if number.get("global_block_id") in attached_numbers:
                    continue
                nb = number.get("block_bbox", [0, 0, 0, 0])
                dy = abs((fb[1] + fb[3]) / 2 - (nb[1] + nb[3]) / 2)
                same_column = reading_column(fb, page_width) == reading_column(nb, page_width)
                if dy <= 38 and same_column and nb[0] >= fb[2] - 5:
                    candidates.append((dy, number))
            if candidates:
                matched = min(candidates, key=lambda item: item[0])[1]
                number_by_formula[formula.get("global_block_id")] = matched
                attached_numbers.add(matched.get("global_block_id"))

        consumed = set()
        for block in blocks:
            block_id = block.get("global_block_id")
            label = block.get("block_label", "text")
            content = block.get("block_content") or ""
            labels[label] += 1
            if block_id in consumed:
                continue
            if label == "table":
                info = table_info(content)
                caption = table_caption_map.get(block_id, "")
                caption_blocks = table_caption_blocks.get(block_id, [])
                note_blocks = table_note_blocks.get(block_id, [])
                related_blocks = [*caption_blocks, *note_blocks]
                records.append({
                    "kind": "table", "text": content, "caption": caption,
                    "footnote": "\n".join(block.get("table_footnote", [])) or "\n".join(plain_text(b.get("block_content", "")) for b in note_blocks),
                    "table": info, "page": page_number, "positions": [block_position(b, page_number) for b in [block, *related_blocks]],
                    "ids": [block_id, *[b.get("global_block_id") for b in related_blocks]],
                })
                continue
            if label in FORMULA_LABELS:
                text = content.strip()
                number = number_by_formula.get(block_id)
                if number:
                    text = f"{text} {number.get('block_content', '').strip()}".strip()
                    consumed.add(number.get("global_block_id"))
                records.append({
                    "kind": "formula", "text": text, "page": page_number,
                    "positions": [block_position(block, page_number)], "ids": [block_id] + ([number.get("global_block_id")] if number else []),
                })
                continue
            if label == "formula_number" and block_id in attached_numbers:
                continue
            if label in IMAGE_LABELS:
                if plain_text(content):
                    records.append({"kind": "text", "text": plain_text(content), "page": page_number, "positions": [block_position(block, page_number)], "ids": [block_id]})
                else:
                    skipped_images.append({"page": page_number, "label": label, "id": block_id})
                continue
            if block_id in attached_captions:
                continue

            text = content.strip()
            if not text:
                continue
            if label in {"figure_title", "figure_table_chart_title", "table_title", "vision_footnote"}:
                text = plain_text(text)
            outline = select_outline(text, page_number, outlines) if label in {"doc_title", "paragraph_title"} else None
            records.append({
                "kind": "heading" if outline else "text", "text": text,
                "outline": outline, "page": page_number,
                "positions": [block_position(block, page_number)], "ids": [block_id],
            })
    return records, skipped_images, labels


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--response", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--token-cache", type=Path, required=True)
    parser.add_argument("--chunk-cap", type=int, default=TOKEN_CAP)
    parser.add_argument("--review-note", action="append", default=[])
    parser.add_argument("--engine-name", default="PaddleOCR-VL-0.9B")
    args = parser.parse_args()

    source_bytes = args.input.read_bytes()
    response_bytes = args.response.read_bytes()
    response = json.loads(response_bytes)
    source_hash = hashlib.sha256(source_bytes).hexdigest()
    response_hash = response.get("source_sha256")
    if response_hash and response_hash != source_hash:
        raise ValueError("Layout response source SHA-256 does not match the input PDF")
    if response.get("errorCode") != 0:
        raise ValueError(f"Layout response error: {response.get('errorMsg')}")

    args.token_cache.mkdir(parents=True, exist_ok=True)
    encoder_path = args.token_cache / "9b5ad71b2ce5302211f9c61530b329a4922fc6a4"
    if not encoder_path.is_file():
        raise FileNotFoundError(f"cl100k_base token cache missing: {encoder_path}")
    os.environ["TIKTOKEN_CACHE_DIR"] = str(args.token_cache.resolve())
    encoder = tiktoken.get_encoding(TOKEN_ENCODING)
    outlines = outline_headings(source_bytes)
    pages = response["result"]["layoutParsingResults"]
    total_layout_blocks = sum(
        len(page.get("prunedResult", {}).get("parsing_res_list", [])) for page in pages
    )
    page_width = pages[0]["prunedResult"]["width"]
    records, skipped_images, label_counts = make_records(response, page_width, outlines)

    title_path = []
    sections = []
    current_body = []

    def flush():
        nonlocal current_body
        if current_body:
            sections.append({"kind": "text", "path": list(title_path), "records": current_body})
            current_body = []

    for record in records:
        if record["kind"] == "heading":
            flush()
            outline = record["outline"]
            level = outline["level"]
            title_path[:] = [part for part in title_path if part["level"] < level]
            title_path.append({"level": level, "text": outline["title"], "page": record["page"]})
            continue
        if record["kind"] != "text":
            flush()
            sections.append({"kind": record["kind"], "path": list(title_path), "records": [record]})
            continue
        current_body.append(record)
    flush()

    chunks = []
    for section in sections:
        path_text = "\n".join("#" * min(item["level"], 6) + " " + item["text"] for item in section["path"])
        if section["kind"] == "table":
            record = section["records"][0]
            text_parts = ([record["caption"]] if record.get("caption") else []) + [record["text"]]
            if record.get("footnote"):
                text_parts.append(record["footnote"])
            content = "\n".join(text_parts)
            combined = (path_text + "\n" + content).strip()
            chunks.append({"kind": "table", "text": combined, "display": record["text"], "caption": record.get("caption", ""), "footnote": record.get("footnote", ""), "path": section["path"], "ids": record["ids"], "pages": [record["page"]], "positions": record["positions"], "table": record["table"], "tokens": len(encoder.encode(combined, disallowed_special=()))})
            continue
        if section["kind"] == "formula":
            record = section["records"][0]
            content = (path_text + "\n" + record["text"]).strip()
            chunks.append({"kind": "formula", "text": content, "display": record["text"], "path": section["path"], "ids": record["ids"], "pages": [record["page"]], "positions": record["positions"], "tokens": len(encoder.encode(content, disallowed_special=()))})
            continue

        prefix = path_text + "\n" if path_text else ""
        current = prefix
        current_ids, current_pages, current_positions = [], [], []

        def append_chunk():
            nonlocal current, current_ids, current_pages, current_positions
            body = current[len(prefix):] if prefix and current.startswith(prefix) else current
            if body.strip():
                chunks.append({
                    "kind": "text", "text": current.strip(), "display": body.strip(), "path": section["path"],
                    "ids": list(dict.fromkeys(current_ids)), "pages": sorted(set(current_pages)),
                    "positions": current_positions.copy(), "tokens": len(encoder.encode(current.strip(), disallowed_special=())),
                })
            current = prefix
            current_ids, current_pages, current_positions = [], [], []

        for record in section["records"]:
            for part in split_text_at_cap(record["text"], max(1, args.chunk_cap - len(encoder.encode(prefix, disallowed_special=()))), encoder):
                candidate = (current + ("\n" if current.strip() else "") + part).strip()
                if current.strip() and len(encoder.encode(candidate, disallowed_special=())) > args.chunk_cap:
                    append_chunk()
                    candidate = (current + part).strip()
                current = (current + ("\n" if current.strip() else "") + part)
                current_ids.extend(record["ids"])
                current_pages.append(record["page"])
                current_positions.extend(record["positions"])
        append_chunk()

    tables = [chunk for chunk in chunks if chunk["kind"] == "table"]
    expected_tables = sum(
        b.get("block_label") == "table"
        for page in pages for b in page["prunedResult"]["parsing_res_list"]
    )
    if len(tables) != expected_tables:
        raise ValueError(f"Table coverage mismatch: expected={expected_tables} output={len(tables)}")
    if len({tuple(chunk["ids"]) for chunk in tables}) != len(tables):
        raise ValueError("A table block was duplicated after chunking")
    if any(chunk["kind"] == "text" and chunk["tokens"] > args.chunk_cap for chunk in chunks):
        raise ValueError("A text chunk exceeds the configured token cap")

    source_records = [record["text"] for record in records if record["kind"] == "text"]
    chunk_body = [chunk["display"] for chunk in chunks if chunk["kind"] == "text"]
    compact = lambda text: re.sub(r"\s+", "", text)
    source_compact = compact("\n".join(source_records))
    chunk_compact = compact("\n".join(chunk_body))
    if source_compact != chunk_compact:
        raise ValueError(
            "Text coverage mismatch after chunking: "
            f"source={len(source_compact)} output={len(chunk_compact)}"
        )

    title = args.input.name
    source_label = "MinerU" if args.engine_name == "MinerU" else "Paddle"
    engine_pipeline = "MinerU pipeline" if args.engine_name == "MinerU" else "PaddleX 文档解析"
    provenance = response.get("provenance", {})
    elapsed = provenance.get("elapsed_seconds")
    elapsed_note = f"本次本地解析耗时 {elapsed:.1f} 秒。" if elapsed is not None else ""
    if response.get("source_sha256"):
        source_note = "解析记录中的源文件 SHA-256 与当前 PDF 校验一致。"
    else:
        source_note = f"Paddle 请求 ID：`{response.get('logId', '未提供')}`；源哈希由本地原始 PDF 计算。"
    lines = [
        f"# 入库验收：{args.engine_name} 切块样本",
        "",
        f"生成时间：{datetime.now().astimezone().isoformat(timespec='seconds')}；解析结果来自本地 {args.engine_name}，使用 {engine_pipeline} 流程。",
        f"源文件：`{title}`，SHA-256：`{source_hash}`。",
        source_note,
        f"解析：{len(pages)} 页，{source_label} 适配后 {total_layout_blocks} 个版面块；过滤页眉页脚等区域后处理 {sum(label_counts.values())} 块。切块：Hierarchy 层级解析（按 PDF 书签匹配标题），正文上限 {args.chunk_cap} tokens，表格/公式保留为原子块。",
        f"识别结果文件：`{args.response}`。{elapsed_note}",
        "",
        "## 结构验收",
        "",
        "| 项目 | 结果 |",
        "| --- | --- |",
        f"| PDF 页数 | {len(pages)} |",
        f"| 目录标题 | {len(outlines)} |",
        f"| {source_label} 表格块 | {len(tables)}，每个已识别的 HTML 对象原样保留为独立块；识别内容准确性另行复核 |",
        f"| 正文块 | {sum(chunk['kind'] == 'text' for chunk in chunks)}，均不超过 {args.chunk_cap} tokens |",
        f"| 公式块 | {sum(chunk['kind'] == 'formula' for chunk in chunks)}，每个公式及编号保持完整 |",
        f"| 图像/图表区域 | {len(skipped_images)} 个图像区域没有正文文本，未复制图像数据 |",
        f"| 正文覆盖 | 相对于本次 OCR 正文输出，{len(source_compact)} 个非空白字符在切块后无丢失、重复或重排；不代表与原 PDF 的识别准确率 |",
        f"| 切块总数 | {len(chunks)} |",
    ]
    if provenance:
        lines.extend(["", "### 本次运行配置", ""])
        if args.engine_name == "MinerU":
            lines.extend([
                f"- MinerU：`{provenance.get('mineru_version', '未提供')}`；backend：`{provenance.get('backend', '未提供')}`；parse_method：`{provenance.get('parse_method', '未提供')}`。",
                f"- 任务 ID：`{provenance.get('task_id', '未提供')}`；开启表格、公式解析，使用本地 MinerU API；未调用 Paddle 重新识别。",
                "- 保留原始 content-list、middle JSON 和 Markdown；仅将类型/位置转为共用切块输入。表格 HTML、表题、表注及公式文本均原样保留。",
                "- 共用切块脚本按 PDF 书签补足章节层级；因此目录校正后的层级效果与 MinerU 原始 text_level 必须分别统计。未执行 RAGFlow 嵌入或索引。",
            ])
        for package in ("paddleocr", "paddlex", "paddlepaddle"):
            version = provenance.get(package + "_version")
            if version:
                lines.append(f"- `{package}`：`{version}`。")
        if args.engine_name == "PP-StructureV3":
            lines.extend([
                "- CPU 本地推理；版面 PP-DocLayout_plus-L、区域 PP-DocBlockLayout、文字 PP-OCRv5_server_det/rec、表格 SLANeXt_wired/SLANet_plus 及单元格检测、公式 PP-FormulaNet_plus-S。",
                "- 关闭文档旋转/去畸变、文本行旋转、印章和图表转数据；保留表格与公式识别。完整参数见同目录 `pipeline.yaml`。",
                "- 逐页 JSON/Markdown 为本次真实 PP-StructureV3 输出；切块由 `paddleocr_service/chunk_pdf.py` 按标题层级及原子表格规则离线生成，未执行 RAGFlow 嵌入或索引。",
            ])
    if args.review_note:
        lines.extend(["", "### 人工复核备注", ""])
        lines.extend(f"- {note}" for note in args.review_note)
    lines.extend([
        "",
        f"正文按 {source_label} 版面阅读顺序排列，并依据 PDF outline 将全文组织为标题层级。切块输出包含各块的标题路径、页码与 {source_label} 源块 ID。长正文按句子和 cl100k token 边界拆分；表格按原 HTML 整表输出，不按 token 拆分。",
        "",
        "### 表格校验",
        "",
        f"| 块号 | 页码 | 行 | 列 | {source_label} 源块 | Tokens | HTML 结构矩形 |",
        "| --- | ---: | ---: | ---: | ---: | ---: | --- |",
    ])
    for index, chunk in enumerate(tables, start=1):
        lines.append(f"| 表格 {index} | {chunk['pages'][0]} | {chunk['table']['rows']} | {chunk['table']['columns']} | {', '.join(map(str, chunk['ids']))} | {chunk['tokens']} | {'是' if chunk['table']['structure_valid'] else '否，需复核'} |")
    lines.extend(["", "表格识别内容：", ""])
    for index, chunk in enumerate(tables, start=1):
        lines.extend([
            f"#### 表格 {index}，第 {chunk['pages'][0]} 页，{chunk['table']['rows']} 行 × {chunk['table']['columns']} 列",
            "",
            f"标题路径：{' / '.join(item['text'] for item in chunk['path']) or '（无）'}",
        ])
        if chunk["caption"]:
            lines.extend([f"表题：{chunk['caption']}", ""])
        lines.extend(["```html", chunk["display"], "```", ""])
        if chunk.get("footnote"):
            lines.extend(["表注：" + chunk["footnote"], ""])

    lines.extend(["## 切块目录", "", "| 块号 | 类型 | 页码 | Tokens | 标题路径 | 源块 ID |", "| --- | --- | --- | ---: | --- | --- |"])
    for index, chunk in enumerate(chunks, start=1):
        path = " / ".join(item["text"] for item in chunk["path"]) or "（无）"
        lines.append(f"| {index:03d} | {chunk['kind']} | {', '.join(map(str, chunk['pages'])) or '-'} | {chunk['tokens']} | {path} | {', '.join(map(str, chunk['ids']))} |")

    lines.extend(["", "## 切块内容", ""])
    for index, chunk in enumerate(chunks, start=1):
        lines.extend(["---", "", f"### Chunk {index:03d} · {chunk['kind']}", "", f"{chunk['tokens']} tokens · 页码 {', '.join(map(str, chunk['pages'])) or '-'} · 源块 {', '.join(map(str, chunk['ids']))}", ""])
        if chunk["kind"] == "table":
            lines.extend(["标题路径：" + " / ".join(item["text"] for item in chunk["path"]), ""])
            if chunk["caption"]:
                lines.extend(["表题：" + chunk["caption"], ""])
            lines.extend(["```html", chunk["display"], "```"])
            if chunk.get("footnote"):
                lines.extend(["", "表注：" + chunk["footnote"]])
        elif chunk["kind"] == "formula":
            lines.extend(["```text", chunk["display"], "```"])
        else:
            lines.append(chunk["text"])
        lines.append("")

    args.output.write_text("\n".join(lines), encoding="utf-8")
    args.response.with_name("chunks.json").write_text(
        json.dumps({"engine": args.engine_name, "source_sha256": source_hash, "chunks": chunks}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps({"output": str(args.output), "pages": len(pages), "chunks": len(chunks), "tables": len(tables), "formulas": sum(c['kind'] == 'formula' for c in chunks), "text_chunks": sum(c['kind'] == 'text' for c in chunks), "max_text_tokens": max((c['tokens'] for c in chunks if c['kind'] == 'text'), default=0), "output_bytes": args.output.stat().st_size}, ensure_ascii=False))


if __name__ == "__main__":
    main()
