"""Drop this function into the caller's tool executor. No RAGFlow key required."""
import json
from urllib import error, request

SEARCH_LIBRARY_TOOL = {
    "name": "search_library", "description": "在一个指定知识库中查找原文，不生成回答。",
    "parameters": {"type": "object", "required": ["library_id", "text"],
        "properties": {
            "library_id": {"type": "string", "description": "入库接口返回的知识库ID"},
            "text": {"type": "string", "maxLength": 8000,
                     "description": "完整检索问题，可附必要背景和限制条件"},
            "mode": {"type": "string", "enum": ["standard", "medium", "high"], "default": "standard"},
            "top_n": {"type": "integer", "minimum": 1, "maximum": 64, "default": 12,
                      "description": "基础检索数量参数，不限制多次查找合并后的总块数"}},
        "additionalProperties": False}}


def search_library(library_id, text, mode="standard", top_n=12, *,
                   access_token, base_url="http://127.0.0.1:30271", caller_id="anonymous"):
    payload = {"library_id": library_id, "text": text, "mode": mode, "top_n": top_n}
    outgoing = request.Request(base_url.rstrip("/") + "/retrieve/manage/v1/search",
        data=json.dumps(payload, ensure_ascii=False).encode(), method="POST",
        headers={"Content-Type": "application/json", "X-Caller-Id": caller_id,
                 "Authorization": f"Bearer {access_token}"})
    try:
        with request.urlopen(outgoing, timeout=940) as response:
            result = json.load(response)
    except error.HTTPError as exc:
        result = json.loads(exc.read())
    if result.get("code") != 0:
        raise RuntimeError(f"{result.get('code')}: {result.get('message')} (request_id={result.get('request_id')})")
    return result
