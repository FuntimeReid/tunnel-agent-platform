#!/usr/bin/env python3
"""Recognize this paper's six table crops with the local PaddleOCR-VL model."""

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import time

ROOT = Path(__file__).resolve().parent.parent
OUT = Path(__file__).resolve().parent
SOURCE = ROOT / "test ocr/2508.11247v1.pdf"
os.environ.setdefault("HF_HOME", str(ROOT / ".tools/paddleocr-home"))
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

import pypdfium2 as pdfium  # noqa: E402
import torch  # noqa: E402
import transformers  # noqa: E402
from transformers import AutoModelForCausalLM, AutoProcessor, TextStreamer  # noqa: E402


def main(model_dir, selected):
    target = OUT / "paddleocr"
    target.mkdir(exist_ok=True)
    source_hash = hashlib.sha256(SOURCE.read_bytes()).hexdigest()
    blocks = json.loads((OUT / "raw/2508.11247v1/auto/2508.11247v1_content_list.json").read_text())
    tables = [b for b in blocks if b["type"] == "table"]
    if len(tables) != 6:
        raise ValueError("Expected the six annotated tables from this paper")
    torch.set_num_threads(4)
    torch.set_num_interop_threads(1)
    print("Loading PaddleOCR-VL on CPU", flush=True)
    model = AutoModelForCausalLM.from_pretrained(
        str(model_dir), trust_remote_code=True, local_files_only=True,
        dtype=torch.float32, attn_implementation="sdpa",
    ).eval()
    # This CPU has AVX512 but no native BF16; dynamic INT8 avoids BF16 emulation.
    model = torch.ao.quantization.quantize_dynamic(model, {torch.nn.Linear}, dtype=torch.qint8)
    processor = AutoProcessor.from_pretrained(
        str(model_dir), trust_remote_code=True, local_files_only=True, use_fast=True,
    )
    digest = hashlib.sha256()
    with (model_dir / "model.safetensors").open("rb") as weights:
        for chunk in iter(lambda: weights.read(1024**2), b""):
            digest.update(chunk)
    weights_hash = digest.hexdigest()
    pdf = pdfium.PdfDocument(SOURCE)
    try:
        for number, block in enumerate(tables, start=1):
            if selected and number not in selected:
                continue
            result_path = target / f"table-{number}.response.json"
            if result_path.exists():
                previous = json.loads(result_path.read_text())
                if previous["source_sha256"] != source_hash or previous["weights_sha256"] != weights_hash:
                    raise ValueError(f"Cached recognition belongs to another source/model: {result_path}")
                print(f"Using saved PaddleOCR recognition for table {number}", flush=True)
                continue
            page = pdf[block["page_idx"]]
            width, height = page.get_size()
            left, top, right, bottom = block["bbox"]
            bbox = [max(0, left * width / 1000 - 3), max(0, top * height / 1000 - 3),
                    min(width, right * width / 1000 + 3), min(height, bottom * height / 1000 + 3)]
            bitmap = page.render(scale=3)
            image = bitmap.to_pil().crop(tuple(round(v * 3) for v in bbox)).convert("RGB")
            bitmap.close()
            messages = [{"role": "user", "content": [
                {"type": "image", "image": image},
                {"type": "text", "text": "Table Recognition:"},
            ]}]
            inputs = processor.apply_chat_template(
                messages, tokenize=True, add_generation_prompt=True,
                return_dict=True, return_tensors="pt",
            )
            # Prefill the first cell marker so decoding continues the OTSL table.
            prefix = torch.tensor([[processor.tokenizer.convert_tokens_to_ids("<fcel>")]])
            inputs["input_ids"] = torch.cat([inputs["input_ids"], prefix], dim=-1)
            inputs["attention_mask"] = torch.cat([inputs["attention_mask"], torch.ones_like(prefix)], dim=-1)
            started = time.monotonic()
            print(f"Recognizing table {number}; in-memory image={image.size}", flush=True)
            with torch.inference_mode():
                output = model.generate(
                    **inputs, max_new_tokens=4096, do_sample=False, use_cache=True,
                    streamer=TextStreamer(processor.tokenizer, skip_prompt=True, skip_special_tokens=True),
                )
            generated = output[0, inputs["input_ids"].shape[-1]:]
            if generated[-1].item() != model.generation_config.eos_token_id:
                raise ValueError(f"Table {number} recognition did not reach EOS")
            result = dict(
                created_at_utc=datetime.now(timezone.utc).isoformat(),
                source_sha256=source_hash, source_page=block["page_idx"] + 1,
                bbox_pdf_points=bbox, crop_size=list(image.size), saved_images=False,
                model="PaddlePaddle/PaddleOCR-VL", weights_sha256=weights_hash,
                model_code_sha256=hashlib.sha256((model_dir / "modeling_paddleocr_vl.py").read_bytes()).hexdigest(),
                transformers_version=transformers.__version__,
                engine="transformers", torch_version=torch.__version__,
                device="cpu", precision="dynamic INT8 linear layers; FP32 other operations",
                prompt="Table Recognition:", generated_tokens=len(generated),
                assistant_prefix="<fcel>", tokenizer_class=type(processor.tokenizer).__name__,
                elapsed_seconds=round(time.monotonic() - started, 3),
                generated_token_ids=generated.tolist(),
                raw="<fcel>" + processor.decode(generated, skip_special_tokens=True),
            )
            result_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
            print(f"Saved {result_path}; {result['elapsed_seconds']}s", flush=True)
            image.close()
            page.close()
    finally:
        pdf.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-dir", type=Path, default=ROOT / ".tools/paddleocr-vl")
    parser.add_argument("--tables", type=int, nargs="*", choices=range(1, 7))
    args = parser.parse_args()
    main(args.model_dir.resolve(), args.tables)
