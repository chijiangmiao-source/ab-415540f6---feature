"""HTTP front-end for the TMS engine: JSON API + static procedure page.

Standard library only, so the image builds with zero external dependencies.
"""

from __future__ import annotations

import json
import os
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .tms import Engine, TmsError

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
FALLBACK_PAGE_DIR = os.path.normpath(os.path.join(BASE_DIR, "..", "web", "src"))

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
}


def make_handler(engine: Engine, page_dir: str, allow_reset: bool):
    class Handler(BaseHTTPRequestHandler):
        server_version = "TmsServer/1.0"
        protocol_version = "HTTP/1.1"

        # --------------------------------------------------------- plumbing

        def log_message(self, fmt, *args):  # keep container logs tidy
            pass

        def _send(self, status, body, content_type="application/json; "
                  "charset=utf-8"):
            if isinstance(body, (dict, list)):
                body = json.dumps(body, ensure_ascii=False).encode("utf-8")
            elif isinstance(body, str):
                body = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _error(self, status, code, message):
            self._send(status, {"error": {"code": code, "message": message}})

        def _json_body(self):
            length = int(self.headers.get("Content-Length") or 0)
            if length == 0:
                return {}
            raw = self.rfile.read(length)
            try:
                return json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                raise TmsError("request body is not valid JSON")

        def _static(self, rel_path):
            for directory in (page_dir, FALLBACK_PAGE_DIR):
                candidate = os.path.normpath(os.path.join(directory, rel_path))
                if candidate.startswith(os.path.normpath(directory)) and \
                        os.path.isfile(candidate):
                    with open(candidate, "rb") as handle:
                        content = handle.read()
                    ext = os.path.splitext(candidate)[1]
                    self._send(200, content,
                               CONTENT_TYPES.get(ext, "application/octet-stream"))
                    return
            self._error(404, "not_found", f"no such page asset: {rel_path}")

        # ------------------------------------------------------------ routes

        def do_GET(self):
            try:
                path = self.path.split("?", 1)[0]
                if path == "/":
                    return self._static("index.html")
                if path in ("/app.js", "/style.css", "/favicon.ico"):
                    return self._static(path.lstrip("/"))
                if path == "/api/healthz":
                    health = engine.health()
                    status = 200 if health["status"] == "ok" else 503
                    health["interface"] = "available" if status == 200 \
                        else "unavailable"
                    return self._send(status, health)
                if path == "/api/state":
                    return self._send(200, engine.state())
                match = re.fullmatch(r"/api/conclusions/([^/]+)/justification",
                                     path)
                if match:
                    return self._send(200, engine.justification(match.group(1)))
                return self._error(404, "not_found", f"no such route: {path}")
            except TmsError as exc:
                self._error(exc.status, exc.code, exc.message)
            except Exception as exc:  # pragma: no cover - defensive
                self._error(500, "internal_error", str(exc))

        def do_POST(self):
            try:
                path = self.path.split("?", 1)[0]
                if path == "/api/facts":
                    body = self._json_body()
                    result = engine.add_fact(body.get("id"),
                                             body.get("label", ""))
                    return self._send(201, result)
                if path == "/api/rules":
                    body = self._json_body()
                    payload = body if isinstance(body, list) \
                        else body.get("rules", body)
                    result = engine.add_rules(payload)
                    return self._send(201, result)
                match = re.fullmatch(r"/api/facts/([^/]+)/(retract|assert)",
                                     path)
                if match:
                    fact_id, action = match.groups()
                    if action == "retract":
                        return self._send(200, engine.retract_fact(fact_id))
                    return self._send(200, engine.assert_fact(fact_id))
                if path == "/api/reset":
                    if not allow_reset:
                        return self._error(403, "forbidden",
                                           "reset is disabled on this server")
                    engine.reset()
                    return self._send(200, {"reset": True})
                return self._error(404, "not_found", f"no such route: {path}")
            except TmsError as exc:
                self._error(exc.status, exc.code, exc.message)
            except Exception as exc:  # pragma: no cover - defensive
                self._error(500, "internal_error", str(exc))

        do_HEAD = do_GET

    return Handler


def main():
    db_path = os.environ.get("TMS_DB_PATH",
                             os.path.join(BASE_DIR, "..", "data", "tms.db"))
    page_dir = os.environ.get("PAGE_DIR", FALLBACK_PAGE_DIR)
    port = int(os.environ.get("PORT", "8000"))
    allow_reset = os.environ.get("TMS_ALLOW_RESET", "0") == "1"

    engine = Engine(db_path)
    handler = make_handler(engine, page_dir, allow_reset)
    server = ThreadingHTTPServer(("0.0.0.0", port), handler)
    print(f"tms-server listening on 0.0.0.0:{port} "
          f"(db={db_path}, page_dir={page_dir}, reset={allow_reset})",
          flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        engine.close()


if __name__ == "__main__":
    main()
