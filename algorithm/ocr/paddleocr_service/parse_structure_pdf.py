#!/usr/bin/env python3
"""Run local PP-StructureV3 and preserve native page results for chunking."""

import argparse
from collections import Counter
from datetime import datetime
import hashlib
import importlib.metadata
import json
from pathlib import Path
import time

import yaml


def save_json(path, value):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    source = args.input.resolve()
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    source_hash = hashlib.sha256(source.read_bytes()).hexdigest()

    import paddlex
    from paddleocr import PPStructureV3

    base = Path(paddlex.__file__).parent / "configs/pipelines/PP-StructureV3.yaml"
    config = yaml.safe_load(base.read_text())
    config["batch_size"] = 1
    config["use_doc_preprocessor"] = False
    config["use_doc_orientation_classify"] = False
    config["use_doc_unwarping"] = False
    config["use_seal_recognition"] = False
    config["use_table_recognition"] = True
    config["use_formula_recognition"] = True
    config["use_chart_recognition"] = False
    config["use_region_detection"] = True
    config["format_block_content"] = True
    # Use the official small formula recognizer for this CPU-only evaluation.
    config["SubPipelines"]["FormulaRecognition"]["SubModules"]["FormulaRecognition"]["model_name"] = "PP-FormulaNet_plus-S"
    model_root = Path(__file__).resolve().parents[1] / ".tools/paddleocr-service-cache/models"

    def pin_models(value):
        if isinstance(value, dict):
            if "model_name" in value:
                model_dir = model_root / value["model_name"]
                if model_dir.is_dir():
                    value["model_dir"] = str(model_dir)
            if "batch_size" in value:
                value["batch_size"] = 1
            if "use_textline_orientation" in value:
                value["use_textline_orientation"] = False
            for child in value.values():
                pin_models(child)
        elif isinstance(value, list):
            for child in value:
                pin_models(child)

    pin_models(config)
    config_file = output / "pipeline.yaml"
    config_file.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    started = time.monotonic()
    pipeline = PPStructureV3(
        paddlex_config=str(config_file), device="cpu", cpu_threads=4,
        enable_mkldnn=False, use_textline_orientation=False,
        use_doc_orientation_classify=False, use_doc_unwarping=False,
    )
    pages = []
    global_id = 0
    print("PP-StructureV3 initialized; starting full PDF inference", flush=True)
    try:
        for index, result in enumerate(pipeline.predict_iter(str(source)), start=1):
            native = result.json["res"]
            save_json(output / f"page-{index:02d}.json", native)
            markdown = result.markdown
            (output / f"page-{index:02d}.md").write_text(
                markdown.get("markdown_texts", ""), encoding="utf-8"
            )
            # Keep native IDs in page JSON; add document-wide IDs in the adapter.
            blocks = []
            for block in native["parsing_res_list"]:
                blocks.append({**block, "global_block_id": global_id})
                global_id += 1
            pruned = {key: native[key] for key in ("width", "height", "page_index", "page_count", "model_settings")}
            pruned["parsing_res_list"] = blocks
            pages.append({"prunedResult": pruned, "markdown": {"text": markdown.get("markdown_texts", "")}})
            save_json(output / "progress.json", {
                "engine": "PP-StructureV3", "source_sha256": source_hash,
                "pages_completed": len(pages), "elapsed_seconds": round(time.monotonic() - started, 2),
            })
            print(json.dumps({"page": index, "blocks": len(blocks), "tables": len(native.get("table_res_list", [])), "elapsed_seconds": round(time.monotonic() - started, 2)}), flush=True)
    finally:
        pipeline.close()
    elapsed = round(time.monotonic() - started, 2)
    response = {
        "errorCode": 0, "errorMsg": "Success", "engine": "PP-StructureV3",
        "source_sha256": source_hash,
        "provenance": {
            "mode": "local_python_pipeline", "source_file": str(source),
            "created_at": datetime.now().astimezone().isoformat(), "elapsed_seconds": elapsed,
            "paddleocr_version": importlib.metadata.version("paddleocr"),
            "paddlex_version": importlib.metadata.version("paddlex"),
            "paddlepaddle_version": importlib.metadata.version("paddlepaddle"),
        },
        "result": {"layoutParsingResults": pages},
    }
    save_json(output / "layout_parsing_response.json", response)
    print(json.dumps({"completed": True, "pages": len(pages), "layout_blocks": global_id, "elapsed_seconds": elapsed, "labels": Counter(b["block_label"] for p in pages for b in p["prunedResult"]["parsing_res_list"])}), flush=True)


if __name__ == "__main__":
    main()
