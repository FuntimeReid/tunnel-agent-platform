#!/usr/bin/env python3
"""Clean this paper using MinerU prose and PaddleOCR-VL structured tables."""

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sys

from lxml import etree, html
from pypdf import PdfReader
import pypdfium2 as pdfium

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUT = Path(__file__).resolve().parent
SOURCE = ROOT / "test ocr/2508.11247v1.pdf"
SAMPLE = ROOT / "test ocr/2508.11247v1-author-tables"
REFERENCE = SAMPLE / "runs/20261028T071618Z/reviewed"
sys.path.insert(0, str(ROOT / "test ocr"))
from run_mineru_author_tables import REGIONS, validate_table  # noqa: E402
from review_mineru_author_tables import native_rows  # noqa: E402
from table_structure import html_to_structure, parse_otsl, table_html as structured_html, table_markdown  # noqa: E402
from latex_formulas import normalize_formulas  # noqa: E402


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def checked_structure(body, region):
    result = validate_table(body, region)
    if region["id"] == "table-6":
        cells = html.fromstring(body).xpath(".//tr[1]/td|.//tr[1]/th")
        # The source Table 6 intentionally has no label over the Time(s) column.
        if len(cells) == 4 and not cells[0].text_content().strip():
            result["issues"] = [i for i in result["issues"] if not i.startswith("1 empty grid cells:")]
            result["intentional_empty_cells"] = [[1, 1]]
            result["status"] = "needs_visual_review" if result["issues"] else "structure_pass_content_unverified"
    return result


def main(output_dir, clean_name):
    out = Path(output_dir).resolve()
    if Path(clean_name).name != clean_name:
        raise ValueError("The output filename must not include directories")
    source_hash = hashlib.sha256(SOURCE.read_bytes()).hexdigest()
    annotated = json.loads((SAMPLE / "sample.json").read_text())
    if source_hash != annotated["source_sha256"]:
        raise ValueError("The PDF differs from the document used to annotate the layout guides")
    raw_root = out / "raw/2508.11247v1/auto"
    markdown = (raw_root / "2508.11247v1.md").read_text()
    content = json.loads((raw_root / "2508.11247v1_content_list.json").read_text())
    blocks = [b for b in content if b.get("type") == "table"]
    matches = list(re.finditer(r"<table\b.*?</table>", markdown, re.S))
    if len(blocks) != 6 or len(matches) != 6:
        raise ValueError(f"Expected six original tables; blocks={len(blocks)}, Markdown tables={len(matches)}")
    observed_pages = sorted({b["page_idx"] + 1 for b in content if "page_idx" in b})
    if observed_pages != list(range(1, 10)):
        raise ValueError(f"Missing source pages in fresh HTTP response: {observed_pages}")
    review = out / "review"
    review.mkdir(exist_ok=True)
    paddle_dir = out / "paddleocr"
    source_pdf = pdfium.PdfDocument(SOURCE)
    regions = [dict(r) for r in REGIONS[1:]] + [
        dict(id="table-6", page=7, bbox=[319, 129, 558, 190], columns=4, rows=2)
    ]
    # Re-extract the author data from this PDF instead of copying the earlier result.
    reader = PdfReader(SOURCE)
    native_captions = {}
    for page in reader.pages:
        for caption in re.findall(r"(?m)^Table ([1-6]):([^\n]*)", page.extract_text()):
            native_captions[int(caption[0])] = f"Table {caption[0]}:{caption[1]}"
    if len(native_captions) != 6:
        raise ValueError("Could not identify the six native table captions")
    native_header = reader.pages[0].extract_text().split("Abstract", 1)[0]
    (review / "authors.native.txt").write_text(native_header)
    lines = native_header.splitlines()
    author_line = next(line for line in lines if re.search(r"\d,.*\d", line))
    authors = [dict(name=m[0].strip(" ,"), affiliation_ids=m[1].split(","))
               for m in re.findall(r"([A-Za-z][A-Za-z .'-]*?)(\d(?:,\d)*)", author_line)]
    affiliations = {m[1]: m[2] for line in lines if (m := re.match(r"^(\d)(\D.+)$", line))}
    if len(authors) != 5 or len(affiliations) != 2 or any(
            key not in affiliations for a in authors for key in a["affiliation_ids"]):
        raise ValueError("Author metadata needs further review")
    emails = [line for line in lines if "@" in line]
    write_json(out / "authors.json", dict(authors=authors, affiliations=affiliations, emails=emails,
                                         source="fresh PDF native text extraction", source_page=1))

    # Reuse coordinate annotations, but recompute every reference value from the PDF.
    guides = json.loads((REFERENCE / "cell-checks.json").read_text())
    guides["table-6"] = dict(source_page=7, column_boundaries=[370, 435, 490, 558], row_baselines=[158.27])
    source_spans = {(1, 0): (3, 1), (4, 0): (3, 1), (7, 0): (3, 1), (10, 0): (1, 2)}
    write_json(review / "layout-guides.json", dict(score_columns=guides,
                                                  table_1_spans=[dict(row=r, column=c, rowspan=rs, colspan=cs)
                                                                 for (r, c), (rs, cs) in source_spans.items()],
                                                  source_sha256=source_hash, annotation_scope="this PDF only"))
    changes = []
    validations = []
    fixed_tables = []
    cell_checks = []
    for region, block, match in zip(regions, blocks, matches):
        name = region["id"]
        number = int(name.split("-")[1])
        captions = block.get("table_caption", [])
        if captions and not any(re.search(rf"Table\s+{number}\s*:", c) for c in captions):
            raise ValueError(f"Table caption order changed for {name}: {captions}")
        if block.get("page_idx") != region["page"] - 1:
            raise ValueError(f"Source page mismatch for {name}")
        if re.sub(r"\s+", " ", match.group(0)).strip() != re.sub(r"\s+", " ", block.get("table_body", "")).strip():
            raise ValueError(f"Markdown/content-list table mismatch for {name}")
        recognition_path = paddle_dir / f"{name}.response.json"
        recognition = json.loads(recognition_path.read_text())
        if recognition["source_sha256"] != source_hash or recognition["source_page"] != region["page"]:
            raise ValueError(f"PaddleOCR recognition source mismatch for {name}")
        header_rows = 2 if name == "table-2" else 1
        recognized = parse_otsl(recognition["raw"], header_rows)
        write_json(paddle_dir / f"{name}.recognized.json", recognized)
        original = structured_html(recognized)
        (review / f"{name}.raw.html").write_text(original)
        before = checked_structure(original, region)
        fixed = original
        guide = guides[name]
        expected = native_rows(source_pdf[guide["source_page"] - 1].get_textpage(),
                               guide["column_boundaries"], guide["row_baselines"])
        tree = html.fromstring(fixed)
        if name == "table-1":
            last = tree.xpath(".//tr[last()]")[0]
            cells = last.xpath("./td|./th")
            if len(cells) == 6 and not cells[0].text_content().strip() and cells[1].text_content().strip() == "HGRAG":
                cells[0].text = "HGRAG"
                cells[0].set("colspan", "2")
                last.remove(cells[1])
                fixed = html.tostring(tree, encoding="unicode")
                changes.append(dict(region=name, action="restore the source HGRAG cell spanning the first two columns"))
            spans = {(c["row"], c["column"]): (c["rowspan"], c["colspan"])
                     for c in html_to_structure(fixed)["cells"]}
            if any(spans.get(k) != v for k, v in source_spans.items()):
                raise ValueError("Table 1 category/HGRAG spans do not match the source annotations")
        if name == "table-6":
            native_label = native_rows(source_pdf[6].get_textpage(), [319, 370], [158.27])[0][0]
            if native_label != "Time(s)":
                raise ValueError("Native Table 6 row label needs review")
            first_row = tree.xpath(".//tr[1]")[0]
            first_cells = first_row.xpath("./td|./th")
            first = first_cells[0]
            second = tree.xpath(".//tr[2]")[0]
            second_cells = second.xpath("./td|./th")
            if len(first_cells) == 3 and first.get("colspan") == "2" and first.text_content().strip() == "HippoRAG 2":
                first.attrib.pop("colspan")
                first_row.insert(0, etree.Element("th"))
                fixed = html.tostring(tree, encoding="unicode")
                changes.append(dict(region=name, action="split the empty first header from the HippoRAG 2 header"))
            if len(first_row.xpath("./td|./th")) != 4 or first_row[0].text_content().strip() or (
                    len(second_cells) != 4 or second_cells[0].text_content().strip() != native_label):
                raise ValueError("Table 6 labels do not match the source")
        if name == "table-3":
            native_label = native_rows(source_pdf[6].get_textpage(), [69, 163], [81.22])[0][0]
            label_cell = tree.xpath(".//tr[2]/td[1]")[0]
            original_label = " ".join(label_cell.itertext()).strip()
            if native_label != "HGRAG w/o Wp":
                raise ValueError("Native Table 3 ablation label needs review")
            if original_label.replace(" ", "") != native_label.replace(" ", ""):
                for child in list(label_cell):
                    label_cell.remove(child)
                label_cell.text = "HGRAG w/o W"
                etree.SubElement(label_cell, "sub").text = "p"
                fixed = html.tostring(tree, encoding="unicode")
                changes.append(dict(region=name, action="restore native subscript p", before=original_label,
                                    after="HGRAG w/o W<sub>p</sub>"))
        rows = tree.xpath(".//tr")[header_rows:]
        if len(rows) != len(expected):
            raise ValueError(f"Unexpected data row count in {name}")
        numeric_repairs = []
        for row_index, (row, expected_row) in enumerate(zip(rows, expected), start=header_rows + 1):
            cells = row.xpath("./td|./th")[-len(expected_row):]
            if len(cells) != len(expected_row):
                raise ValueError(f"Missing score columns in {name} row {row_index}")
            for col, (cell, value) in enumerate(zip(cells, expected_row), start=1):
                actual = " ".join(cell.itertext()).strip()
                if actual != value:
                    numeric_repairs.append(dict(row=row_index, numeric_column=col, before=actual, after=value))
                    for child in list(cell):
                        cell.remove(child)
                    cell.text = value
        if numeric_repairs:
            fixed = html.tostring(tree, encoding="unicode")
            changes.append(dict(region=name, action="restore cells using PDF native character boxes", cells=numeric_repairs))
        after = checked_structure(fixed, region)
        if after["issues"]:
            raise ValueError(f"Unresolved table structure in {name}: {after['issues']}")
        verified = [[" ".join(c.itertext()).strip() for c in row.xpath("./td|./th")][-len(expected[0]):]
                    for row in html.fromstring(fixed).xpath(".//tr")[header_rows:]]
        if verified != expected:
            raise ValueError(f"Final score mismatch in {name}")
        cell_checks.append(dict(region=name, checked_cells=sum(map(len, expected)), cells=expected,
                                status="matches_fresh_native_text"))
        if captions != [native_captions[number]]:
            changes.append(dict(region=name, action="associate the native caption with its own table",
                                before=captions, after=native_captions[number]))
        validations.append(dict(region=name, source_page=region["page"], caption=native_captions[number],
                                original_caption_association=captions,
                                before=before, after=after, changed=fixed != original))
        structure = html_to_structure(fixed)
        # Retain the source spans in the structured result and HTML review.
        fixed = structured_html(structure)
        write_json(paddle_dir / f"{name}.json", dict(
            source_page=region["page"], recognition_file=recognition_path.name,
            caption=native_captions[number], **structure,
        ))
        (review / f"{name}.html").write_text(fixed)
        final_check = checked_structure(fixed, region)
        if final_check["issues"]:
            raise ValueError(f"Reconstructed table is invalid: {name}: {final_check['issues']}")
        display = structure
        if name == "table-1":
            display = {**structure, "cells": [dict(cell) for cell in structure["cells"]]}
            for cell in display["cells"]:
                if (cell["row"], cell["column"]) == (0, 0):
                    cell.update(text="Category", colspan=1)
                elif (cell["row"], cell["column"]) == (10, 0):
                    cell.update(column=1, colspan=1)
            display["cells"].extend([
                dict(row=0, column=1, rowspan=1, colspan=1, text="Method", header=True),
                dict(row=10, column=0, rowspan=1, colspan=1, text="", header=False),
            ])
        fixed_tables.append(table_markdown(display))

    # Replace verified tables before applying the PDF-reviewed formula repairs.
    clean = re.sub(r"(?m)^Table [1-6]:[^\n]*(?:\n|$)", "", markdown)
    replacements = iter(enumerate(fixed_tables, start=1))

    def replace_table(match):
        number, fixed = next(replacements)
        return "\n" + fixed + "\n\n" + native_captions[number] + "\n"

    clean = re.sub(r"<table\b.*?</table>", replace_table, clean, flags=re.S)
    abstract = re.search(r"(?m)^(?:#{1,6}\s*)?Abstract\s*$", clean)
    if not abstract or "![" in clean[:abstract.start()]:
        raise ValueError("Unexpected first-page structure; review the author replacement boundary")
    title = " ".join(lines[:2])
    header = [f"# {title}", "", "论文作者信息：  "]
    for author in authors:
        units = "、".join(f"单位{key}" for key in author["affiliation_ids"])
        header.append(f"{author['name']}（{units}）  ")
    header.append("")
    for key, value in affiliations.items():
        header.append(f"单位{key}：{value}  ")
    header.extend(["", "## Abstract", "", ""])
    clean = "\n".join(header) + clean[abstract.end():].lstrip("\n")
    reference_blocks = [b for b in content if b.get("sub_type") == "ref_text"]
    reference_pages = sorted({b["page_idx"] + 1 for b in reference_blocks})
    reference_items = [item for block in reference_blocks for item in block["list_items"]]
    if reference_pages != [8, 9] or not reference_items or not reader.pages[7].extract_text().startswith("References\n"):
        raise ValueError("Could not verify the reference section on pages 8 and 9")
    first_reference = reference_items[0]
    if clean.count(first_reference) != 1:
        raise ValueError("Could not uniquely locate the first reference")
    reference_start = clean.index(first_reference)
    if " ".join(clean[reference_start:].split()) != " ".join("\n\n".join(reference_items).split()):
        raise ValueError("The document tail does not match the complete reference section")
    clean = re.sub(r"(?m)^#{1,6}\s+References\s*$", "", clean[:reference_start]).rstrip() + "\n"
    changes.append(dict(region="references", action="remove the complete bibliography; retain in-text citations",
                        source_pages=reference_pages))
    figures = [block for block in content if block["type"] == "image"]
    for figure in figures:
        for caption in figure.get("image_caption", []) + figure.get("image_footnote", []):
            if clean.count(caption) != 1:
                raise ValueError(f"Could not uniquely locate figure caption: {caption[:80]}")
            clean = clean.replace(caption, "", 1)
    clean, removed_images = re.subn(r"!\[[^\]]*\]\([^\n)]+\)[ \t]*", "", clean)
    clean = re.sub(r"\n(?:[ \t]*\n){2,}", "\n\n", clean)
    if removed_images != len(figures) or re.search(r"<img\b|<sup\b|(?m:^Figure\s+\d+\s*:)", clean):
        raise ValueError("Figure/author cleanup needs review")
    clean, formula_validation = normalize_formulas(clean, source_hash)
    write_json(out / "formula-validation.json", formula_validation)
    for directory in (out / "images", review):
        if directory.exists():
            for image in directory.iterdir():
                if image.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}:
                    image.unlink()
            if directory.name == "images" and not any(directory.iterdir()):
                directory.rmdir()
    (out / "raw.md").write_text(markdown)
    if re.search(r"(?m)^#{1,6}\s+References\s*$", clean) or any(item in clean for item in reference_items):
        raise ValueError("Reference section content remains after cleanup")
    clean_path = out / clean_name
    clean_path.write_text(clean.rstrip() + "\n")
    write_json(out / "validation.json", dict(tables=validations, cells=cell_checks,
                                             total_checked_cells=sum(c["checked_cells"] for c in cell_checks),
                                             table_format="Markdown",
                                             formulas=dict(inline_count=formula_validation["inline_count"],
                                                           display_count=formula_validation["display_count"],
                                                           report="formula-validation.json"),
                                             removed_figures=len(figures), saved_images=False,
                                             references_removed=True, reference_pages=reference_pages,
                                             author_format="姓名（单位编号）；单位编号：机构名称"))
    write_json(out / "repairs.json", dict(author_source="fresh PDF native text", table_repairs=changes,
                                          table_recognizer="PaddlePaddle/PaddleOCR-VL; local CPU inference",
                                          validation_source="PDF native characters and document-specific layout annotations",
                                          formula_rules="latex_rules.json", formula_validation="formula-validation.json",
                                          saved_images=False))
    response = json.loads((out / "raw/response-metadata.json").read_text())
    write_json(out / "manifest.json", dict(
        created_at_utc=datetime.now(timezone.utc).isoformat(), source_sha256=source_hash,
        mineru_endpoint=response["endpoint"], status_code=response["status_code"],
        mineru_version=json.loads((out / "raw/health.json").read_text())["version"],
        mineru_request_parameters=response["fields"],
        source_pages=observed_pages, author_count=len(authors), table_count=6,
        table_recognizer="PaddlePaddle/PaddleOCR-VL",
        table_model_weights_sha256=recognition["weights_sha256"],
        table_pipeline="in-memory crops -> PaddleOCR-VL OTSL -> explicit cells with spans -> native-text checks -> Markdown",
        table_format="Markdown",
        formula_format="LaTeX math in Markdown",
        inline_formula_count=formula_validation["inline_count"],
        display_formula_count=formula_validation["display_count"],
        formula_rules_sha256=hashlib.sha256((DEFAULT_OUT / "latex_rules.json").read_bytes()).hexdigest(),
        formula_script_sha256=hashlib.sha256((DEFAULT_OUT / "latex_formulas.py").read_bytes()).hexdigest(),
        removed_figures=len(figures), saved_images=False,
        references_removed=True, reference_pages=reference_pages,
        checked_numeric_and_dash_cells=sum(c["checked_cells"] for c in cell_checks),
        clean_file=clean_name,
        clean_sha256=hashlib.sha256(clean_path.read_bytes()).hexdigest(),
        clean_script_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        scope="body from pages 1-7; bibliography on pages 8-9 removed; in-text citations retained; plain author affiliations; figures/captions removed; PaddleOCR-VL tables; PDF-reviewed LaTeX formulas; other prose from MinerU output",
    ))
    source_pdf.close()
    print(f"Saved {clean_path}; 9 pages, 5 authors, 6 tables, {sum(c['checked_cells'] for c in cell_checks)} checked cells")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--filename", default="clean.md")
    args = parser.parse_args()
    main(args.output_dir, args.filename)
