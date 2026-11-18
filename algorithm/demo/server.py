"""Local UI and streaming proxy for the existing ingestion gateway (stdlib only)."""
import argparse
import http.client
import json
import logging
import mimetypes
import re
import socket
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote, urlsplit

ROOT = Path(__file__).resolve().parent
LOG = logging.getLogger("midterm-demo")
ROUTES = {
    "/api/health": "/ingest/health",
    "/api/libraries": "/retrieve/libraries",
    "/api/directions": "/retrieve/directions",
    "/api/documents": "/ingest/documents",
}


class Handler(BaseHTTPRequestHandler):
    server_version = "TunnelDemo/1.0"

    def log_message(self, fmt, *args):
        LOG.info("%s %s", self.client_address[0], fmt % args)

    def json_response(self, status, data):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self.dispatch()

    def do_POST(self):
        self.dispatch()

    def dispatch(self):
        parsed = urlsplit(self.path)
        if parsed.path == "/api/config" and self.command == "GET":
            return self.json_response(200, {"code": 0, "data": {
                "ragflow_url": self.server.ragflow_url,
                "max_files": 10000, "upload_batch_size": 10, "max_file_bytes": 125829120,
                "poll_seconds": 5,
            }})
        if parsed.path == "/api/demo-state" and self.command == "GET":
            saved = ROOT / "demo_state.json"
            return self.json_response(200, {"code": 0, "data": json.loads(saved.read_text(encoding="utf-8")) if saved.exists() else None})
        upstream = ROUTES.get(parsed.path)
        if not upstream and re.fullmatch(r"/api/documents/[a-fA-F0-9]{32}(?:/retry)?", parsed.path):
            upstream = "/ingest" + parsed.path.removeprefix("/api")
        if upstream:
            allowed = (self.command == "GET" and not upstream.endswith("/retry")) or (
                self.command == "POST" and (upstream in ("/retrieve/libraries", "/retrieve/directions", "/ingest/documents") or upstream.endswith("/retry")))
            if not allowed:
                return self.json_response(405, {"code": "method_not_allowed", "message": "该操作不支持此请求方式"})
            return self.proxy(upstream + ("?" + parsed.query if parsed.query else ""))
        if self.command != "GET":
            return self.json_response(404, {"code": "not_found", "message": "接口不存在"})
        files = {"/": ROOT / "web/index.html", "/app.js": ROOT / "web/app.js", "/styles.css": ROOT / "web/styles.css"}
        path = files.get(parsed.path)
        is_sample = parsed.path.startswith("/samples/")
        if is_sample:
            from urllib.parse import unquote
            name = unquote(parsed.path.removeprefix("/samples/"))
            if name == Path(name).name and name not in ("", ".", ".."):
                path = ROOT / "samples" / name
        if not path or not path.is_file():
            return self.json_response(404, {"code": "not_found", "message": "文件不存在"})
        body = path.read_bytes()
        self.send_response(200)
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        self.send_header("Content-Type", mime + ("; charset=utf-8" if mime.startswith("text/") or path.suffix == ".js" else ""))
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if is_sample:
            self.send_header("Content-Disposition", "attachment; filename*=UTF-8''" + quote(path.name))
        self.end_headers()
        self.wfile.write(body)

    def proxy(self, path):
        target = urlsplit(self.server.gateway_url)
        conn = None
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length < 0 or length > 1200 * 1024 * 1024:
                return self.json_response(413, {"code": "file_too_large", "message": "单次请求过大"})
            cls = http.client.HTTPSConnection if target.scheme == "https" else http.client.HTTPConnection
            conn = cls(target.hostname, target.port, timeout=10)
            conn.connect()
            conn.sock.settimeout(900 if self.command == "POST" else 35)
            conn.putrequest(self.command, target.path.rstrip("/") + path)
            conn.putheader("Content-Length", str(length))
            conn.putheader("X-Caller-Id", "guanbo-midterm-demo")
            if self.headers.get("Content-Type"):
                conn.putheader("Content-Type", self.headers["Content-Type"])
            conn.endheaders()
            remaining = length
            while remaining:
                chunk = self.rfile.read(min(256 * 1024, remaining))
                if not chunk:
                    raise ConnectionError("上传连接中断")
                conn.send(chunk)
                remaining -= len(chunk)
            response = conn.getresponse()
            body = response.read()
            self.send_response(response.status)
            self.send_header("Content-Type", response.getheader("Content-Type", "application/json; charset=utf-8"))
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        except (OSError, ValueError, http.client.HTTPException, socket.timeout) as exc:
            LOG.warning("gateway request failed: %s", type(exc).__name__)
            self.json_response(502, {"code": "gateway_unavailable", "message": "暂时无法连接服务器，请检查网络后重试。", "detail": type(exc).__name__})
        finally:
            if conn:
                conn.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=18080)
    parser.add_argument("--gateway-url", default="http://127.0.0.1:30268")
    parser.add_argument("--ragflow-url", default="http://127.0.0.1:28080")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    server.gateway_url = args.gateway_url.rstrip("/")
    server.ragflow_url = args.ragflow_url.rstrip("/")
    LOG.info("Open http://127.0.0.1:%s", args.port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
