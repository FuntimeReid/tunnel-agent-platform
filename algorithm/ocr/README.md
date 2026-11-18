# OCR 和结构化切块

## PaddleOCR

[paddleocr_service/](paddleocr_service/) 保存 PaddleOCR-VL 服务、PP-StructureV3 解析与 PDF 切块代码。服务用 PaddleX 在 CPU 上做版面识别。切块时保留页码和内容类型，处理前置区作者信息和跨页续表。

```bash
cd algorithm/ocr/paddleocr_service
python -m pip install -r requirements.in requests pypdfium2 tiktoken lxml
bash serve.sh
```

第一次运行需要相应的 Paddle 模型。可设置 `PADDLEOCR_PIPELINE` 使用自己的模型目录配置，或用 `PADDLEOCR_HOST`、`PADDLEOCR_PORT` 改监听地址。通过 `parse_pdf.py --input <pdf> --output <解析结果.json>` 调 PaddleOCR-VL，再用 `chunk_pdf.py --input <pdf> --response <解析结果.json> --output <输出> --token-cache <缓存目录>` 切块。通用的 PP-StructureV3 入口是 `parse_structure_pdf.py --input <pdf> --output-dir <输出目录>`。

RAGFlow 的 PaddleOCR 适配代码见 [paddleocr_parser.py](../ragflow/deepdoc/parser/paddleocr_parser.py) 和 [OCR 模型接口](../ragflow/rag/llm/ocr_model.py)。入库网关当前配置默认沿用 MinerU 路线；要使用 PaddleOCR，按相应 RAGFlow 环境配置解析器。

## 表格与公式

[structured_cleanup/](structured_cleanup/) 保存表格结构重建、LaTeX 检查和按标题预览切块的代码。里面的解析对照脚本沿用原验证的输入布局，复跑时自行提供 PDF、解析结果和本地模型。
