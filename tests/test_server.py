from __future__ import annotations

import json
import unittest
import urllib.error
import urllib.request
from unittest.mock import Mock

from symphony.server import StatusServer


class StatusServerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.orchestrator = Mock()
        self.orchestrator.snapshot.return_value = {
            "generated_at": "2026-04-30T00:00:00+00:00",
            "counts": {"running": 0, "retrying": 0},
            "running": [],
            "retrying": [],
            "codex_totals": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0, "seconds_running": 0},
            "rate_limits": None,
        }
        self.orchestrator.issue_snapshot.return_value = None
        self.orchestrator.request_tick.return_value = False
        self.server = StatusServer(self.orchestrator, "127.0.0.1", 0)
        await self.server.start()
        host, port = self.server.server_address
        self.base = f"http://{host}:{port}"

    async def asyncTearDown(self):
        await self.server.stop()

    def test_state_and_refresh_endpoints(self):
        with urllib.request.urlopen(f"{self.base}/api/v1/state", timeout=5) as response:
            payload = json.loads(response.read().decode("utf-8"))
        self.assertEqual(payload["counts"], {"running": 0, "retrying": 0})

        request = urllib.request.Request(f"{self.base}/api/v1/refresh", data=b"{}", method="POST")
        with urllib.request.urlopen(request, timeout=5) as response:
            self.assertEqual(response.status, 202)
            payload = json.loads(response.read().decode("utf-8"))
        self.assertTrue(payload["queued"])

    def test_unsupported_method_on_defined_route_returns_405(self):
        with self.assertRaises(urllib.error.HTTPError) as err:
            urllib.request.urlopen(f"{self.base}/api/v1/refresh", timeout=5)
        try:
            self.assertEqual(err.exception.code, 405)
        finally:
            err.exception.close()


if __name__ == "__main__":
    unittest.main()
