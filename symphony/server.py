from __future__ import annotations

import asyncio
import json
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from urllib.parse import unquote, urlparse

from .orchestrator import Orchestrator


class _Handler(BaseHTTPRequestHandler):
    orchestrator: Orchestrator

    def log_message(self, format: str, *args):  # noqa: A002
        return

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/":
            self._send_html(self._dashboard_html())
            return
        if path == "/api/v1/refresh":
            self._send_json({"error": {"code": "method_not_allowed", "message": "use POST for refresh"}}, HTTPStatus.METHOD_NOT_ALLOWED)
            return
        if path == "/api/v1/state":
            self._send_json(self.orchestrator.snapshot())
            return
        if path.startswith("/api/v1/"):
            identifier = unquote(path.removeprefix("/api/v1/"))
            snapshot = self.orchestrator.issue_snapshot(identifier)
            if snapshot is None:
                self._send_json({"error": {"code": "issue_not_found", "message": "issue not found"}}, HTTPStatus.NOT_FOUND)
            else:
                self._send_json(snapshot)
            return
        self._send_json({"error": {"code": "not_found", "message": "not found"}}, HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if path != "/api/v1/refresh":
            self._send_json({"error": {"code": "method_not_allowed", "message": "unsupported route"}}, HTTPStatus.METHOD_NOT_ALLOWED)
            return
        coalesced = self.orchestrator.request_tick()
        self._send_json({"queued": True, "coalesced": coalesced, "operations": ["poll", "reconcile"]}, HTTPStatus.ACCEPTED)

    def do_PUT(self) -> None:
        self._method_not_allowed()

    def do_PATCH(self) -> None:
        self._method_not_allowed()

    def do_DELETE(self) -> None:
        self._method_not_allowed()

    def _method_not_allowed(self) -> None:
        self._send_json({"error": {"code": "method_not_allowed", "message": "method not allowed"}}, HTTPStatus.METHOD_NOT_ALLOWED)

    def _send_json(self, payload: dict, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload, sort_keys=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, body: str) -> None:
        data = body.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _dashboard_html(self) -> str:
        snapshot = self.orchestrator.snapshot()
        return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>Symphony</title>
<style>body{{font-family:system-ui,sans-serif;margin:2rem;}}pre{{background:#f6f8fa;padding:1rem;overflow:auto;}}</style>
</head><body><h1>Symphony</h1><pre>{json.dumps(snapshot, indent=2)}</pre></body></html>"""


class StatusServer:
    def __init__(self, orchestrator: Orchestrator, host: str, port: int):
        handler = type("SymphonyHandler", (_Handler,), {"orchestrator": orchestrator})
        self.httpd = ThreadingHTTPServer((host, port), handler)
        self.thread = Thread(target=self.httpd.serve_forever, daemon=True)

    @property
    def server_address(self):
        return self.httpd.server_address

    async def start(self) -> None:
        self.thread.start()

    async def stop(self) -> None:
        await asyncio.to_thread(self.httpd.shutdown)
        self.thread.join(timeout=2)
        self.httpd.server_close()
