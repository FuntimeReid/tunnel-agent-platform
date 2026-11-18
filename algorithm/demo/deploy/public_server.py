"""Server-hosted UI: scoped ingestion and read-only native retrieval.

RAGFlow's API key is read from its existing server secret and never sent to a
visitor.
"""
import argparse
import cgi
import http.client
import json
import logging
from pathlib import Path
import re
import secrets
import sys
import threading
from http.server import ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from server import Handler as LocalHandler, ROOT

SCOPE_LOCK = threading.Lock()
ID = r"[a-f0-9]{32}"
REPORT = "中期检查报告_资料入库与原文检索_2026-11-10"


def call(url, method="GET", body=None, headers=None):
    target = urlsplit(url)
    conn = http.client.HTTPConnection(target.hostname, target.port, timeout=900)
    try:
        conn.request(method, target.path + ("?" + target.query if target.query else ""), body, headers or {})
        r = conn.getresponse()
        return r.status, r.getheader("Content-Type", "application/json"), r.read()
    finally:
        conn.close()


class Handler(LocalHandler):
    def do_PATCH(self):
        self.dispatch()

    def output(self, status, body, content_type="text/html; charset=utf-8", headers=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def allowed_ids(self):
        with SCOPE_LOCK:
            return set(self.server.scope["libraries"])

    def remember(self, kind, values):
        with SCOPE_LOCK:
            self.server.scope[kind] = sorted(set(self.server.scope[kind]) | set(values))
            p = ROOT / "public_state.json"
            temporary = p.with_suffix(".tmp")
            temporary.write_text(json.dumps(self.server.scope, ensure_ascii=False), encoding="utf-8")
            temporary.replace(p)

    def read_json(self):
        length = int(self.headers.get("Content-Length", "0"))
        if length < 0 or length > 131072:
            raise ValueError("请求内容过长")
        return json.loads(self.rfile.read(length))

    def dispatch(self):
        try:
            path = urlsplit(self.path).path
            if path.startswith('/retrieve/'):
                return self.management_proxy()
            if path.startswith('/api/') and self.command != 'GET':
                return self.json_response(401, {'code': 'login_required', 'message': '请登录后，通过管理接口提交操作。'})
            if path == "/demo/login":
                return self.output(303, b"", headers={"Location": "/"})
            if self.command == "POST" and self.headers.get("Origin") not in (None, self.server.public_url):
                return self.json_response(403, {"code": "forbidden", "message": "请求来源无效。"})
            if path.startswith("/api/v1/"):
                return self.native_api(path)
            if path.startswith(("/dataset/", "/entry/", "/chunk/", "/assets/", "/locales/", "/fonts/")) or path in ("/logo.svg", "/iconfont.js", "/favicon.ico"):
                return self.native_page(path)
            if path in ("/reports/report.pdf", "/reports/report.docx") and self.command == "GET":
                ext = path.rsplit(".", 1)[1]
                p = ROOT / (REPORT + "." + ext)
                return self.output(200, p.read_bytes(), "application/pdf" if ext == "pdf" else "application/vnd.openxmlformats-officedocument.wordprocessingml.document", {"Content-Disposition": "inline; filename*=UTF-8''" + quote(p.name)})
            if path == "/api/config":
                return self.json_response(200, {"code": 0, "data": {"ragflow_url": "", "public_demo": True, "max_files": 10000, "upload_batch_size": 10, "max_file_bytes": 125829120, "poll_seconds": 5}})
            if path == "/api/retrieve" and self.command == "POST":
                return self.retrieve()
            if path == "/api/shared-records":
                with SCOPE_LOCK:
                    ids = list(self.server.scope["records"])
                records = []
                for start in range(0, len(ids), 100):
                    _, _, raw = call(self.server.gateway_url + "/ingest/documents?ids=" + ",".join(ids[start:start+100]))
                    records += json.loads(raw).get("data", {}).get("records", [])
                return self.json_response(200, {"code": 0, "data": {"records": records}})
            return super().dispatch()
        except (ValueError, KeyError, TypeError) as exc:
            return self.json_response(400, {"code": "invalid_request", "message": str(exc)[:160]})
        except (OSError, http.client.HTTPException):
            logging.exception("Demo upstream failure")
            return self.json_response(502, {"code": "upstream_unavailable", "message": "服务器暂时无法完成操作，请稍后重试。"})

    def management_proxy(self):
        """HTTPS API entry for the system team's frontend; retain auth headers."""
        length = int(self.headers.get('Content-Length', '0'))
        if length < 0 or length > 257 * 1048576:
            return self.json_response(413, {'code': 'file_too_large', 'message': '请求内容过大。'})
        headers = {name: self.headers[name] for name in ('Content-Type', 'Authorization', 'Idempotency-Key', 'X-Caller-Id')
                   if self.headers.get(name)}
        # Large approved uploads may take time to register upstream.
        target = urlsplit(self.server.gateway_url)
        connection = http.client.HTTPConnection(target.hostname, target.port, timeout=930)
        try:
            connection.putrequest(self.command, self.path)
            for name, value in headers.items():
                connection.putheader(name, value)
            connection.putheader('Content-Length', str(length))
            connection.endheaders()
            remaining = length
            while remaining:
                chunk = self.rfile.read(min(262144, remaining))
                if not chunk:
                    raise ValueError('请求内容不完整。')
                remaining -= len(chunk)
                connection.send(chunk)
            response = connection.getresponse()
            return self.output(response.status, response.read(), response.getheader('Content-Type', 'application/json'))
        finally:
            connection.close()

    def native_page(self, path):
        if self.command != "GET":
            return self.json_response(405, {"code": "read_only", "message": "这里只展示检索页面。"})
        match = re.fullmatch(r"/dataset/retrieval/(" + ID + r")", path)
        if path.startswith("/dataset/") and (not match or match[1] not in self.allowed_ids()):
            return self.json_response(403, {"code": "outside_demo", "message": "无法访问该知识库。"})
        status, mime, body = call(self.server.ragflow_url + self.path)
        if match and status == 200:
            # This value is only a browser-side UI marker. It is not a real
            # RAGFlow credential; only allowlisted requests get a server key.
            boot = '''<script>localStorage.setItem('Authorization','public-demo-ui-marker');localStorage.setItem('token','public-demo-ui-marker');localStorage.setItem('userInfo',JSON.stringify({name:'访客',nickname:'访客',email:'',avatar:''}));localStorage.setItem('lng','zh');</script>'''
            banner = '''<a href="/" style="position:fixed;right:20px;top:10px;z-index:99999;background:#14685e;color:white;padding:8px 18px;border-radius:8px;font:14px system-ui;text-decoration:none">返回资料入库</a>'''
            body = body.replace(b"<head>", ("<head>" + boot).encode(), 1).replace(b"</body>", (banner + "</body>").encode(), 1)
        return self.output(status, body, mime)

    def retrieve(self):
        payload = self.read_json()
        library_id = payload.get("library_id")
        text = payload.get("text")
        mode = payload.get("mode", "standard")
        top_n = payload.get("top_n", 12)
        if library_id not in self.allowed_ids():
            return self.json_response(403, {"code": "outside_demo", "message": "请选择可用的知识库。"})
        if not isinstance(text, str) or not 0 < len(text.strip()) <= 8000:
            raise ValueError("请输入问题，最多8000字。")
        if mode not in ("standard", "medium", "high"):
            raise ValueError("检索档位无效。")
        if type(top_n) is not int or not 1 <= top_n <= 64:
            raise ValueError("原文块数量须为1至64。")
        request_body = {"text": text.strip(), "library_id": library_id,
                        "mode": mode, "top_n": top_n}
        status, _, raw = call(self.server.gateway_url + "/retrieve/search", "POST",
                              json.dumps(request_body, ensure_ascii=False).encode(),
                              {"Content-Type": "application/json", "X-Caller-Id": "web-ui"})
        result = json.loads(raw)
        if status != 200 or result.get("code") != 0:
            return self.json_response(status, result)
        return self.json_response(200, result)

    def native_api(self, path):
        payload = None
        if path == "/api/v1/users/me" and self.command == "GET":
            return self.json_response(200, {"code": 0, "data": {"id": "demo", "nickname": "访客", "email": "", "avatar": "", "language": "zh", "is_superuser": False}})
        if path == "/api/v1/tenants" and self.command == "GET":
            return self.json_response(200, {"code": 0, "data": []})
        # Only the names needed for Testing's dropdown; no provider config/key.
        if path in ("/api/v1/models", "/api/v1/models/default") and self.command == "GET":
            return self.json_response(200, {"code": 0, "data": [] if path.endswith("models") else {}})
        if path == "/api/v1/datasets/search" and self.command == "POST":
            payload = self.read_json()
            ids = payload.get("dataset_ids") or payload.get("kb_id") or []
            if isinstance(ids, str):
                ids = [ids]
            if not ids or len(ids) != 1 or not set(ids) <= self.allowed_ids():
                return self.json_response(403, {"code": "outside_demo", "message": "请选择可用的知识库。"})
            if not isinstance(payload.get("question"), str) or not 0 < len(payload["question"]) <= 8000:
                raise ValueError("问题不能为空，且最多8000字。")
            payload["page_size"] = min(int(payload.get("page_size", 10)), 30)
            payload["rerank_candidates_count"] = min(int(payload.get("rerank_candidates_count", 64)), 128)
        elif path == "/api/v1/datasets" and self.command == "GET":
            pass
        else:
            match = re.fullmatch(r"/api/v1/datasets/(" + ID + r")(?:/documents(?:/" + ID + r"(?:/chunks)?)?|/tags|/metadata)?", path)
            if self.command != "GET" or not match or match[1] not in self.allowed_ids():
                return self.json_response(403, {"code": "read_only", "message": "当前操作不可用。"})
        body = json.dumps(payload).encode() if payload is not None else None
        status, mime, raw = call(self.server.ragflow_url + self.path, self.command, body, {"Authorization": "Bearer " + self.server.api_key, "Content-Type": "application/json"})
        if path == "/api/v1/datasets" and status == 200:
            result = json.loads(raw)
            if isinstance(result.get("data"), list):
                result["data"] = [x for x in result["data"] if x.get("id") in self.allowed_ids()]
                result["total_datasets"] = len(result["data"])
            return self.json_response(status, result)
        return self.output(status, raw, mime)

    def proxy(self, path):
        parsed = urlsplit(path)
        body = None
        content_type = self.headers.get("Content-Type", "application/json")
        if parsed.path == "/ingest/documents" and self.command == "POST":
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 1200 * 1024 * 1024:
                raise ValueError("上传请求大小超出限制。")
            # FieldStorage spools uploaded files to disk rather than keeping
            # large PDFs in memory. Only rebuild ordinary file uploads.
            form = cgi.FieldStorage(fp=self.rfile, headers=self.headers, environ={"REQUEST_METHOD": "POST", "CONTENT_TYPE": content_type, "CONTENT_LENGTH": str(length)})
            library_id = form.getfirst("library_id", "")
            if library_id not in self.allowed_ids():
                return self.json_response(403, {"code": "outside_demo", "message": "无法向该知识库上传资料。"})
            files = [x for x in form.list or [] if x.name == "file" and x.filename]
            if not 1 <= len(files) <= 10:
                raise ValueError("每次请选择1至10份文件。")
            boundary = "demo-" + secrets.token_hex(16)
            segments = []
            for name, value in (("library_id", library_id), ("category", form.getfirst("category", "general"))):
                segments.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode())
            for f in files:
                name = Path(f.filename).name.replace('"', "_").replace("\r", "_").replace("\n", "_")
                f.file.seek(0, 2)
                size = f.file.tell()
                f.file.seek(0)
                if not 0 < size <= 125829120:
                    raise ValueError("单个文件不能为空或超过120 MiB。")
                segments.extend([f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{name}"\r\nContent-Type: application/octet-stream\r\n\r\n'.encode(), (f.file, size), b"\r\n"])
            segments.append(f"--{boundary}--\r\n".encode())
            target = urlsplit(self.server.gateway_url)
            conn = http.client.HTTPConnection(target.hostname, target.port, timeout=900)
            try:
                conn.putrequest("POST", path)
                conn.putheader("Content-Type", "multipart/form-data; boundary=" + boundary)
                conn.putheader("X-Caller-Id", "guanbo-shared-midterm-demo")
                conn.putheader("Content-Length", str(sum(len(x) if isinstance(x, bytes) else x[1] for x in segments)))
                conn.endheaders()
                for segment in segments:
                    if isinstance(segment, bytes):
                        conn.send(segment)
                    else:
                        while chunk := segment[0].read(262144):
                            conn.send(chunk)
                response = conn.getresponse()
                status, raw = response.status, response.read()
            finally:
                conn.close()
        else:
            if self.command == "POST" and parsed.path == "/retrieve/directions":
                body = json.dumps(self.read_json(), ensure_ascii=False).encode()
            elif self.command == "POST" and parsed.path in ("/ingest/libraries", "/retrieve/libraries"):
                payload = self.read_json()
                _, _, all_raw = call(self.server.gateway_url + "/ingest/libraries")
                existing = json.loads(all_raw).get("data", {}).get("libraries", [])
                if any(x.get("name") == str(payload.get("name", "")).strip() and x.get("id") not in self.allowed_ids() for x in existing):
                    return self.json_response(403, {"code": "outside_demo", "message": "知识库名称已被使用。"})
                body = json.dumps(payload, ensure_ascii=False).encode()
            elif parsed.path == "/ingest/documents" and self.command == "GET":
                ids = parse_qs(parsed.query).get("ids", [""])[0].split(",")
                if not set(ids) <= set(self.server.scope["records"]):
                    return self.json_response(403, {"code": "outside_demo", "message": "无法查看该记录。"})
            elif parsed.path.startswith("/ingest/documents/"):
                rid = parsed.path.split("/")[3]
                if rid not in self.server.scope["records"]:
                    return self.json_response(403, {"code": "outside_demo", "message": "无法操作该记录。"})
            status, _, raw = call(self.server.gateway_url + path, self.command, body, {"Content-Type": content_type, "X-Caller-Id": "guanbo-shared-midterm-demo"})
        result = json.loads(raw)
        if status < 300 and result.get("code") == 0:
            data = result.get("data") or {}
            if parsed.path in ("/ingest/libraries", "/retrieve/libraries"):
                if self.command == "POST":
                    self.remember("libraries", [data["id"]])
                else:
                    data["libraries"] = [x for x in data.get("libraries", []) if x.get("id") in self.allowed_ids()]
            elif parsed.path == "/ingest/documents" and self.command == "POST":
                self.remember("records", [x["record_id"] for x in data.get("items", []) if x.get("record_id")])
        return self.json_response(status, result)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=30272)
    parser.add_argument("--gateway-url", default="http://127.0.0.1:30268")
    parser.add_argument("--ragflow-url", default="http://127.0.0.1:8080")
    parser.add_argument("--public-url", default="http://127.0.0.1:30272")
    parser.add_argument("--api-key-file", type=Path, required=True)
    args = parser.parse_args()
    scope = {"libraries": [], "records": []}
    saved = ROOT / "public_state.json"
    if saved.exists():
        scope = json.loads(saved.read_text(encoding="utf-8"))
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    server.scope = scope
    server.api_key = args.api_key_file.read_text(encoding="utf-8").strip()
    server.gateway_url = args.gateway_url.rstrip("/")
    server.ragflow_url = args.ragflow_url.rstrip("/")
    server.public_url = args.public_url.rstrip("/")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    logging.info("Server demo listening on 127.0.0.1:%s", args.port)
    server.serve_forever()


if __name__ == "__main__":
    main()
