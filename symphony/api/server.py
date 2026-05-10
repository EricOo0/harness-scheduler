from __future__ import annotations

import asyncio
from http.server import ThreadingHTTPServer
from pathlib import Path
from threading import Event, Thread

from symphony.api.app import HarnessApp
from symphony.api.tasks import HarnessRequestHandler
from symphony.storage.db import HarnessPaths


class HarnessServer:
    def __init__(self, app: HarnessApp, host: str, port: int):
        self.app = app
        handler = type("HarnessHandler", (HarnessRequestHandler,), {"app": app})
        self.httpd = ThreadingHTTPServer((host, port), handler)
        self.thread = Thread(target=self.httpd.serve_forever, daemon=True)
        self.scheduler_stop = Event()
        self.scheduler_thread = Thread(target=self._mock_scheduler_loop, daemon=True)

    @property
    def server_address(self):
        return self.httpd.server_address

    async def start(self) -> None:
        self.thread.start()
        self.scheduler_thread.start()

    async def stop(self) -> None:
        self.scheduler_stop.set()
        await asyncio.to_thread(self.httpd.shutdown)
        self.thread.join(timeout=2)
        self.scheduler_thread.join(timeout=2)
        self.httpd.server_close()

    def _mock_scheduler_loop(self) -> None:
        while not self.scheduler_stop.wait(1.0):
            try:
                self.app.process_dispatchable_once()
            except Exception:
                continue


async def run_harness_server(*, project_root: str | Path, host: str = "127.0.0.1", port: int = 8765, data_dir: str | None = None) -> HarnessServer:
    app = HarnessApp(HarnessPaths.from_project(project_root, data_dir))
    server = HarnessServer(app, host, port)
    await server.start()
    return server
