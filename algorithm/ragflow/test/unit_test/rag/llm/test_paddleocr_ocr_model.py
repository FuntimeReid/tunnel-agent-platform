import json

import pytest


def test_paddleocr_model_uses_configured_request_timeout():
    from rag.llm.ocr_model import PaddleOCROcrModel

    model = PaddleOCROcrModel(
        key=json.dumps({
            "paddleocr_base_url": "http://127.0.0.1:18888",
            "paddleocr_request_timeout": 7200,
        }),
        model_name="PaddleOCR-VL",
    )

    assert model.request_timeout == 7200


def test_paddleocr_model_rejects_nonpositive_request_timeout():
    from rag.llm.ocr_model import PaddleOCROcrModel

    with pytest.raises(ValueError, match="PADDLEOCR_REQUEST_TIMEOUT"):
        PaddleOCROcrModel(
            key=json.dumps({"paddleocr_request_timeout": 0}),
            model_name="PaddleOCR-VL",
        )
